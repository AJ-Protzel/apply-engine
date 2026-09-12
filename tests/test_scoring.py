"""Ranking tests.

The model's two numbers are an input here, not a thing under test -- these run
against hand-written `Score` objects, so every decision the pipeline makes from
a score is verified without an API call. That is the point of keeping the
judgment behind a boundary: the policy is cheap to test, and the policy is what
gets tuned.

These run against the real `config/profile.yaml`, like the filter tests, so a
threshold change shows up here rather than in a thin digest three days later.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime

import pytest

from apply_engine import scoring
from apply_engine.config import load_profile
from apply_engine.models import Job

PROFILE = load_profile()
DAY = date(2026, 9, 14)


def job(title: str = "Data Analyst", *, company: str = "Example Health",
        location: str | None = "Sacramento, CA", posted: datetime | None = None) -> Job:
    return Job(
        source="greenhouse",
        source_job_id=f"{company}-{title}",
        company=company,
        title=title,
        location_raw=location,
        region="ca-norcal",
        employment_type="full_time",
        description="Build reports and maintain data pipelines.",
        apply_url=f"https://boards.greenhouse.io/example/jobs/{abs(hash(title)) % 9999}",
        posted_at=posted or datetime(2026, 9, 13, tzinfo=UTC),
    )


def score(fit: int = 9, compounding: int = 4, **kwargs) -> scoring.Score:
    kwargs.setdefault("verdict", "Core analytics work at a regional employer.")
    return scoring.Score(fit=fit, compounding=compounding, **kwargs)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("fit", "compounding", "expected"),
    [
        (8, 3, None),                                  # exactly on both thresholds
        (10, 5, None),
        (9, 4, None),
        (7, 5, "below_fit_threshold"),                 # one short on fit
        (10, 2, "below_compounding_threshold"),        # one short on compounding
        (10, 1, "below_compounding_threshold"),        # the tempting one
        (4, 1, "below_fit_threshold"),                  # both short: fit is the problem
        (1, 1, "below_fit_threshold"),
    ],
)
def test_gate(fit, compounding, expected):
    assert scoring.gate(score(fit, compounding), PROFILE) == expected


def test_gate_reads_the_thresholds_from_config():
    """Both numbers are config because both will be wrong on the first pass."""
    loose = {"volume": {"fit_threshold": 5, "compounding_threshold": 1}}
    strict = {"volume": {"fit_threshold": 10, "compounding_threshold": 5}}

    assert scoring.gate(score(6, 2), loose) is None
    assert scoring.gate(score(9, 4), strict) == "below_fit_threshold"


def test_gate_falls_back_to_the_documented_defaults():
    assert scoring.gate(score(8, 3), {}) is None
    assert scoring.gate(score(7, 3), {}) == "below_fit_threshold"


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Analytics Engineer", 1),
        ("Data Analyst", 1),
        ("Business Systems Analyst", 2),
        ("Salesforce Administrator", 3),
        ("Program Analyst", 4),
        ("Analytics Engineer II", 1),          # substring, so suffixes still match
        ("Underwater Basket Weaver", None),    # unlisted is not a filter, just no tier
    ],
)
def test_title_tier(title, expected):
    assert scoring.title_tier(title, PROFILE) == expected


def test_title_tier_prefers_the_longest_match():
    """"Data Operations Analyst" is tier 1 and contains tier 2's "Operations
    Analyst". First-match-wins would file it as tier 2."""
    assert scoring.title_tier("Data Operations Analyst", PROFILE) == 1


def test_title_tier_ignores_non_tier_keys():
    profile = {"titles": {"tier_1": ["Data Analyst"], "preferred": ["Something Else"],
                          "tier_not_a_number": ["Data Analyst"]}}
    assert scoring.title_tier("Data Analyst", profile) == 1
    assert scoring.title_tier("Something Else", profile) is None


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

def test_a_clean_night_queues_the_survivors_best_first():
    scored = [
        (job("Data Analyst"), score(8, 3)),
        (job("Analytics Engineer"), score(10, 5)),
        (job("Reporting Analyst"), score(9, 4)),
        (job("Line Cook"), score(2, 1)),
    ]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)

    assert [r.job.title for r in plan.queued] == [
        "Analytics Engineer", "Reporting Analyst", "Data Analyst",
    ]
    assert [r.job.title for r in plan.skipped] == ["Line Cook"]
    assert plan.daily_remaining == 2
    assert plan.weekly_remaining == 22


def test_overflow_is_deferred_not_skipped():
    """The distinction that matters: a 9 that came sixth is still a 9 tomorrow."""
    scored = [(job(f"Data Analyst {n}"), score(9, 4)) for n in range(8)]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)

    assert len(plan.queued) == 5
    assert len(plan.deferred) == 3
    assert not plan.skipped, "losing to a cap is not a rejection"
    assert {r.reason for r in plan.deferred} == {"daily_cap_reached"}


def test_slots_already_spent_today_come_off_the_cap():
    scored = [(job(f"Data Analyst {n}"), score(9, 4)) for n in range(5)]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY, queued_today=3)

    assert len(plan.queued) == 2
    assert len(plan.deferred) == 3
    assert plan.daily_remaining == 0


def test_the_weekly_cap_binds_and_says_so():
    """Five a weekday is 25 a week exactly, so catch-up activity spends the week."""
    scored = [(job(f"Data Analyst {n}"), score(9, 4)) for n in range(5)]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY, queued_this_week=23)

    assert len(plan.queued) == 2
    assert {r.reason for r in plan.deferred} == {"weekly_cap_reached"}
    assert plan.weekly_remaining == 0


def test_a_full_week_queues_nothing_and_defers_everything():
    scored = [(job("Analytics Engineer"), score(10, 5))]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY, queued_this_week=25)

    assert not plan.queued
    assert plan.deferred[0].reason == "weekly_cap_reached"
    assert plan.weekly_remaining == 0


def test_a_cap_already_overspent_does_not_go_negative():
    scored = [(job("Analytics Engineer"), score(10, 5))]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY, queued_today=9, queued_this_week=40)

    assert plan.daily_remaining == 0
    assert plan.weekly_remaining == 0


def test_the_order_is_stable_whatever_order_the_scores_arrive_in():
    """Without a total order the digest stops reproducing and tuning dies."""
    scored = [
        (job("Analytics Engineer"), score(9, 4)),
        (job("Data Analyst"), score(9, 4)),
        (job("Salesforce Administrator"), score(9, 4)),
        (job("Program Analyst"), score(9, 4)),
        (job("Business Systems Analyst"), score(9, 4)),
    ]
    baseline = [r.job.title for r in scoring.plan_queue(scored, PROFILE, day=DAY).ranked]

    shuffled = list(scored)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert [
            r.job.title for r in scoring.plan_queue(shuffled, PROFILE, day=DAY).ranked
        ] == baseline


def test_tier_breaks_a_tie_on_both_numbers():
    scored = [
        (job("Salesforce Administrator"), score(9, 4)),  # tier 3
        (job("Analytics Engineer"), score(9, 4)),        # tier 1
    ]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)
    assert [r.job.title for r in plan.queued] == [
        "Analytics Engineer", "Salesforce Administrator",
    ]


def test_the_newer_posting_breaks_a_tie_on_tier():
    old = job("Data Analyst", company="Older", posted=datetime(2026, 1, 1, tzinfo=UTC))
    new = job("Data Analyst", company="Newer", posted=datetime(2026, 9, 1, tzinfo=UTC))
    plan = scoring.plan_queue([(old, score()), (new, score())], PROFILE, day=DAY)
    assert [r.job.company for r in plan.queued] == ["Newer", "Older"]


def test_a_posting_with_no_date_sorts_last_rather_than_crashing():
    undated = job("Data Analyst", company="Undated")
    undated.posted_at = None
    dated = job("Data Analyst", company="Dated")
    plan = scoring.plan_queue([(undated, score()), (dated, score())], PROFILE, day=DAY)
    assert [r.job.company for r in plan.queued] == ["Dated", "Undated"]


def test_an_empty_night_is_a_valid_plan():
    plan = scoring.plan_queue([], PROFILE, day=DAY)
    assert plan.ranked == []
    assert plan.daily_remaining == 5


def test_tempting_is_only_the_low_compounding_rejection():
    scored = [
        (job("Reporting Analyst"), score(10, 1)),   # tempting
        (job("Line Cook"), score(2, 1)),            # also low compounding, but no fit
        (job("Data Analyst"), score(6, 5)),         # fails on fit alone
    ]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)
    tempting = [r.job.title for r in plan.skipped if r.tempting]

    assert "Reporting Analyst" in tempting
    assert "Data Analyst" not in tempting


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------

def test_summary_line():
    scored = [(job(f"Data Analyst {n}"), score(9, 4)) for n in range(6)]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)
    assert scoring.summary_line(plan) == "5 queued · 1 deferred"
    assert scoring.summary_line(plan, held=2).endswith("2 held")


def test_summary_line_on_a_night_with_nothing():
    plan = scoring.plan_queue([], PROFILE, day=DAY)
    assert scoring.summary_line(plan) == "nothing queued"


def test_the_digest_leads_with_the_queue_and_names_the_temptations():
    scored = [
        (job("Analytics Engineer"), score(10, 5, builds="dbt and Snowflake in production")),
        (job("Reporting Analyst"), score(10, 1, concerns="Excel-only reporting seat.")),
        (job("Line Cook"), score(1, 1)),
    ]
    plan = scoring.plan_queue(scored, PROFILE, day=DAY)
    held = [(job("Contract Analyst", company="Blue Shield of California"),
             "recruiter_conflict: TEKsystems submitted 2026-08-14")]
    text = scoring.render_digest(plan, held=held)

    assert text.startswith("2026-09-14 — 1 queued · 1 held")
    assert text.index("QUEUED") < text.index("SKIPPED")
    assert "Analytics Engineer" in text
    assert "builds: dbt and Snowflake in production" in text
    assert "right job, wrong rung" in text
    assert "Reporting Analyst" in text
    assert "concerns: Excel-only reporting seat." in text
    assert "TEKsystems" in text
    # An ordinary rejection is a count, not an entry.
    assert "Line Cook" not in text
    assert "Also scored and skipped: 1 (below_fit_threshold 1)" in text
    assert "Slots left: 4 today, 24 this week." in text


def test_the_digest_carries_a_link_for_everything_it_lists():
    scored = [(job("Analytics Engineer"), score(10, 5)),
              (job("Reporting Analyst"), score(10, 1))]
    text = scoring.render_digest(scoring.plan_queue(scored, PROFILE, day=DAY))
    assert text.count("https://boards.greenhouse.io/") == 2


def test_the_digest_shows_soft_flags():
    scored = [(job("Solutions Engineer"), score(8, 3, soft_flags=["Check for quota language."]))]
    text = scoring.render_digest(scoring.plan_queue(scored, PROFILE, day=DAY))
    assert "flag: Check for quota language." in text


def test_the_digest_of_an_empty_night_still_reports_the_slots():
    text = scoring.render_digest(scoring.plan_queue([], PROFILE, day=DAY))
    assert "nothing queued" in text
    assert "Slots left: 5 today, 25 this week." in text


def test_the_digest_names_a_posting_with_no_location():
    nowhere = job("Data Analyst", location=None)
    text = scoring.render_digest(
        scoring.plan_queue([(nowhere, score())], PROFILE, day=DAY)
    )
    assert "ca-norcal" in text  # falls back to the classified region


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_carries_the_posting_and_the_target_titles():
    prompt = scoring.scoring_prompt(job("Analytics Engineer"), PROFILE)

    assert "Analytics Engineer" in prompt
    assert "Example Health" in prompt
    assert "Sacramento, CA" in prompt
    assert "tier_1:" in prompt and "tier_4:" in prompt
    assert "fit (1-10)" in prompt and "compounding (1-5)" in prompt
    assert "Stated salary: not stated" in prompt
    # The candidate summary defaults to the committed bullet bank, so the model
    # scores against the same text the resume claims.
    assert "Oregon State University" in prompt


def test_the_prompt_formats_a_stated_range():
    paid = job("Data Analyst")
    paid.salary_min, paid.salary_max = 70_000, 90_000
    assert "Stated salary: 70,000-90,000" in scoring.scoring_prompt(paid, PROFILE)


def test_the_prompt_truncates_a_long_description():
    """Requirements are at the top; benefits boilerplate is not worth paying for."""
    verbose = job("Data Analyst")
    verbose.description = "x" * 20_000
    prompt = scoring.scoring_prompt(verbose, PROFILE, description_limit=500)

    assert "[truncated]" in prompt
    assert len(prompt) < 5_000


def test_the_prompt_accepts_an_explicit_summary():
    prompt = scoring.scoring_prompt(job(), PROFILE, bullets={"summary_base": "A test person."})
    assert "A test person." in prompt
    assert "Oregon State University" not in prompt


def test_a_score_out_of_range_is_rejected():
    """A model asked for 1-10 will occasionally answer 0 or 11."""
    with pytest.raises(ValueError):
        scoring.Score(fit=0, compounding=3, verdict="nope")
    with pytest.raises(ValueError):
        scoring.Score(fit=11, compounding=3, verdict="nope")
    with pytest.raises(ValueError):
        scoring.Score(fit=9, compounding=6, verdict="nope")
