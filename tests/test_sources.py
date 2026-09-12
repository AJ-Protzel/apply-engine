"""Connector tests against recorded live payloads.

Every file in `fixtures/` is a real response from the real endpoint, captured
once and trimmed (long strings truncated, two postings kept per source). No test
here touches the network: `get_json` is patched per module, so `fetch()` runs
end to end offline -- including the parts that are easy to forget, like Workable
resolving the employer name out of the payload and RemoteOK's first row being a
legal notice rather than a job.

This suite exists because of what it found. "Returns an empty list" and "is
broken" look identical from the outside, and two connectors were quietly wrong
in exactly that way:

  * Workable returned `location_raw=None` for every posting it parsed. The
    payload has no `location` key -- the place is at the top level as
    `city`/`state`/`country` -- and `geography_kill` deliberately keeps postings
    whose location it cannot read, so a London claims job would have passed the
    geography filter as readily as a Sacramento one. Latent rather than live:
    `companies.yaml` never held a Workable slug, so this was waiting for the
    first Workable employer to be added.

  * Recruitee returned `posted_at=None` for every posting, because it stamps
    dates as `2026-07-29 08:20:35 UTC` and `fromisoformat` rejects the trailing
    zone name. This one was live -- 4 postings a night, all undated -- and a
    null date is indistinguishable from a source that publishes no date, so
    nothing surfaced it.

Both are asserted below, on real payloads, so neither can come back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apply_engine import filters, normalize
from apply_engine.config import load_profile
from apply_engine.sources import (
    REMOTE_ONLY_SOURCES,
    ashby,
    greenhouse,
    lever,
    recruitee,
    remoteok,
    remotive,
    workable,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = load_profile()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def offline(monkeypatch):
    """Patch a connector's `get_json` to serve a fixture instead of the network.

    Each module does `from .base import get_json`, so the name to patch lives on
    the module, not on `base`.
    """

    def install(module, payload):
        def fake_get_json(url, *, params=None, headers=None):
            return payload

        monkeypatch.setattr(module, "get_json", fake_get_json)

    return install


# ---------------------------------------------------------------------------
# Per-company boards
# ---------------------------------------------------------------------------

def test_greenhouse_parses_a_real_board(offline):
    offline(greenhouse, fixture("greenhouse"))
    jobs = greenhouse.fetch("algolia", "Algolia")

    assert [j.title.strip() for j in jobs] == [
        "Account Manager - Dutch speaking",
        "Business Development Representative - Dutch Speaker",
    ]
    first = jobs[0]
    assert first.source == "greenhouse"
    assert first.company == "Algolia"
    assert first.source_job_id == "6141481004"
    assert first.location_raw == "London, England"
    assert first.apply_url.startswith("https://job-boards.greenhouse.io/algolia/")
    assert first.posted_at is not None
    assert first.description  # content=true, so the description arrives in the list call


def test_lever_flattens_the_lists_section(offline):
    offline(lever, fixture("lever"))
    jobs = lever.fetch("metabase", "Metabase")

    analytics = next(j for j in jobs if j.title == "Analytics Engineer")
    assert analytics.location_raw == "Remote-North & South America"
    assert analytics.employment_type_raw == "Full-time (remote)"
    assert analytics.apply_url.startswith("https://jobs.lever.co/metabase/")
    # createdAt is epoch milliseconds, not a string.
    assert analytics.posted_at is not None and analytics.posted_at.year == 2020

    ci = next(j for j in jobs if j.title == "CI Engineer")
    assert len(ci.description) > len(analytics.description), (
        "the lists[] sections should be appended to the description"
    )


def test_ashby_reads_nested_compensation(offline):
    offline(ashby, fixture("ashby"))
    jobs = ashby.fetch("airbyte", "Airbyte")

    support = next(j for j in jobs if j.title.startswith("Customer Support"))
    assert (support.salary_min, support.salary_max) == (99_000, 115_000)
    assert support.employment_type_raw == "FullTime"
    assert support.location_raw == "United States"

    manager = next(j for j in jobs if j.title == "Engineering Manager, Platform")
    assert manager.salary_min == 217_000


def test_workable_finds_the_location_that_is_not_under_location(offline):
    """The regression that mattered. See the module docstring."""
    offline(workable, fixture("workable"))
    jobs = workable.fetch("zego")

    assert all(j.company == "Zego" for j in jobs), "employer name comes from the payload"
    assert [j.location_raw for j in jobs] == [
        "London, England, United Kingdom",
        "Halifax, England, United Kingdom",
    ]
    assert all(j.location_raw is not None for j in jobs)


def test_workable_prefers_the_remote_flag_over_a_city():
    entry = {
        "shortcode": "X1",
        "title": "Data Analyst",
        "url": "https://apply.workable.com/j/X1",
        "telecommuting": True,
        "city": "London",
        "country": "United Kingdom",
    }
    assert workable._parse(entry, "zego", "Zego").location_raw == "Remote"


def test_workable_falls_back_to_the_locations_list():
    entry = {
        "shortcode": "X2",
        "title": "Data Analyst",
        "url": "https://apply.workable.com/j/X2",
        "locations": [
            {"city": "Hidden", "region": "Nowhere", "hidden": True},
            {"city": "Sacramento", "region": "CA", "country": "United States"},
        ],
    }
    parsed = workable._parse(entry, "zego", "Zego")
    assert parsed.location_raw == "Sacramento, CA, United States"


def test_recruitee_parses_its_non_iso_timestamps(offline):
    """The other regression: `2026-07-29 08:20:35 UTC` is not ISO 8601."""
    offline(recruitee, fixture("recruitee"))
    jobs = recruitee.fetch("spring", "Spring Health")

    assert all(j.posted_at is not None for j in jobs)
    assert jobs[0].posted_at.year == 2026
    assert jobs[0].posted_at.tzinfo is not None
    assert jobs[0].location_raw.startswith("Lyon,")
    assert jobs[0].location_raw.endswith("FR")
    assert jobs[0].employment_type_raw == "internship"


def test_recruitee_marks_remote_offers():
    entry = {
        "id": 1,
        "title": "Data Analyst",
        "careers_url": "https://spring.recruitee.com/o/data-analyst",
        "remote": True,
        "city": "Paris",
        "country_code": "FR",
        "published_at": "2026-07-29 08:20:35 UTC",
    }
    assert recruitee._parse(entry, "spring", None).location_raw.startswith("Remote - Paris")


# ---------------------------------------------------------------------------
# Board-wide feeds
# ---------------------------------------------------------------------------

def test_remotive_dedupes_across_categories(offline):
    """One posting listed under two categories must arrive once.

    `fetch` queries five categories with the same call. The fixture is served to
    every one of them, so a missing dedupe shows up here as five copies.
    """
    offline(remotive, fixture("remotive"))
    jobs = remotive.fetch()

    assert len(jobs) == 2
    assert len({j.source_job_id for j in jobs}) == 2
    assert jobs[0].location_raw  # candidate_required_location, or "Remote"


def test_remoteok_drops_the_legal_notice_row(offline):
    """RemoteOK's first element is an attribution notice, not a posting."""
    payload = fixture("remoteok")
    assert "id" not in payload[0], "fixture should still contain the notice row"

    offline(remoteok, payload)
    jobs = remoteok.fetch()

    assert len(jobs) == len(payload) - 1
    assert all(j.source == "remoteok" for j in jobs)


# ---------------------------------------------------------------------------
# Through the whole pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "module", "args", "expected_source"),
    [
        ("greenhouse", greenhouse, ("algolia", "Algolia"), "greenhouse"),
        ("lever", lever, ("metabase", "Metabase"), "lever"),
        ("ashby", ashby, ("airbyte", "Airbyte"), "ashby"),
        ("workable", workable, ("zego", None), "workable"),
        ("recruitee", recruitee, ("spring", "Spring Health"), "recruitee"),
    ],
)
def test_every_connector_normalizes(offline, name, module, args, expected_source):
    """A RawJob from each source must survive normalization into a valid Job."""
    offline(module, fixture(name))
    for raw in module.fetch(*args):
        job = normalize.normalize(raw, remote_hint=raw.source in REMOTE_ONLY_SOURCES)
        assert job.source == expected_source
        assert job.title and job.company and job.apply_url
        assert job.region in {"remote-us", "ca-norcal", "ca-other", "wa", "other"}


def test_a_london_posting_is_killed_on_geography(offline):
    """What the Workable fix actually buys.

    With `location_raw` null -- the old behaviour -- `geography_kill` returns
    None and a UK posting passes the filter, because the rule keeps what it
    cannot read. With the location parsed, it is killed for being outside the
    allowed regions. This is the assertion that makes the bug fix load-bearing
    rather than cosmetic.
    """
    offline(workable, fixture("workable"))
    claims = next(
        normalize.normalize(raw)
        for raw in workable.fetch("zego")
        if raw.title.startswith("Claims Handler")
    )

    assert claims.region == "other"
    result = filters.evaluate(claims, PROFILE)
    assert not result.passed
    assert result.kill_rule == "geography:outside_allowed_region:other"
