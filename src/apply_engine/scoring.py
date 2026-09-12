"""The ranking layer: everything that happens to a posting after it survives the
hard filters, minus the one part that needs judgment.

The split is the whole design. A model reads a job description and returns a
`Score` -- two numbers and a sentence. Every *decision* made from that score is
ordinary code in this file: the gate, the daily and weekly caps, the ordering,
what the digest says. The judgment call stays in one place and stays small, and
the policy around it is deterministic, diffable, and tested.

Nothing here calls a model or touches the network. `scoring_prompt()` renders
the prompt, `Score` validates what comes back, and the scheduled session that
owns the API call sits between them. That boundary is also what makes the queue
rules testable without spending a cent.

Three decisions in here were wrong the first time:

1.  **Losing to a cap is not the same as being rejected.** Both leave a job
    un-queued tonight, so the first version recorded both as `skipped`. But a
    job that scored 9 and came eleventh on a five-slot night is still a 9
    tomorrow, and marking it skipped removed it from consideration forever. Cap
    overflow is `deferred`: no `applications` row is written at all, so the job
    is simply unqueued and competes again on the next run.

2.  **The gate reports which threshold failed, not that one did.** `fit` being
    one short and `compounding` being one short call for opposite corrections --
    widen the title list, or raise the bar on what counts as compounding. A
    single `below_threshold` reason cannot tell you which.

3.  **High fit with low compounding gets named out loud.** It is the one
    rejection a tired person overturns by hand at 11pm, so the digest prints it
    under its own heading with the reason attached instead of letting it vanish
    into a rejection count.

    Which is why a posting failing BOTH thresholds is recorded against fit, not
    against compounding. The first version named the compounding half whenever
    it failed, on the theory that "right job, wrong rung" is the more actionable
    correction -- and the heading promptly filled up with line-cook postings
    that scored 1 on fit, where the rung is beside the point. The heading is
    only worth reading if everything under it is a job worth wanting.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import load_bullets
from .models import Job

Outcome = Literal["queued", "deferred", "skipped"]

# Checked in this order, and the first failure is the recorded one. Fit comes
# first so that `below_compounding_threshold` means exactly one thing: a posting
# that cleared fit and still would not build anything.
SKIP_REASONS = ("below_fit_threshold", "below_compounding_threshold")


class Score(BaseModel):
    """What the model returns for one posting. Mirrors the `job_scores` table.

    The bounds are enforced here and again by CHECK constraints in the schema.
    That duplication is deliberate: a model asked for 1-10 will occasionally
    answer 0 or 11, and the right outcome is a rejected score rather than a row
    that quietly widens what the column means.
    """

    fit: int = Field(ge=1, le=10)
    compounding: int = Field(ge=1, le=5)
    verdict: str
    title_bucket: str | None = None
    builds: str | None = None
    concerns: str | None = None
    soft_flags: list[str] = Field(default_factory=list)
    model: str | None = None


class Ranked(BaseModel):
    """A posting, its score, and what this module decided to do with it."""

    job: Job
    score: Score
    outcome: Outcome
    reason: str | None = None
    tier: int | None = None

    @property
    def tempting(self) -> bool:
        """Strong fit, weak compounding.

        The rejection most likely to be overturned by hand, so the digest names
        it rather than counting it.
        """
        return self.outcome == "skipped" and self.reason == "below_compounding_threshold"


class QueuePlan(BaseModel):
    """One run's decisions, ready to write and to render."""

    ranked: list[Ranked] = Field(default_factory=list)
    day: date
    daily_remaining: int
    weekly_remaining: int

    @property
    def queued(self) -> list[Ranked]:
        return [r for r in self.ranked if r.outcome == "queued"]

    @property
    def deferred(self) -> list[Ranked]:
        return [r for r in self.ranked if r.outcome == "deferred"]

    @property
    def skipped(self) -> list[Ranked]:
        return [r for r in self.ranked if r.outcome == "skipped"]


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def title_tier(title: str, profile: dict[str, Any]) -> int | None:
    """Which tier in `profile.yaml` this title belongs to, or None.

    Tier is a tie-break and a reporting bucket, never a filter -- an unlisted
    title can still score a 9 and queue ahead of a tier-1 title that scored 8.
    The buckets exist so the weekly readout can say which KIND of role replies,
    which is a different question from which role is worth applying to.

    Longest listed title wins, not first listed: "Junior Data Engineer" must not
    be claimed by the shorter "Data Engineer" simply because it appears earlier.
    """
    haystack = title.casefold()
    best: tuple[int, int] | None = None  # (length of the matched name, tier)

    for key, listed in sorted((profile.get("titles") or {}).items()):
        if not key.startswith("tier_"):
            continue
        try:
            tier = int(key.removeprefix("tier_"))
        except ValueError:
            continue
        for name in listed or []:
            needle = str(name).casefold()
            if needle and needle in haystack and (best is None or len(needle) > best[0]):
                best = (len(needle), tier)

    return best[1] if best else None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def gate(score: Score, profile: dict[str, Any]) -> str | None:
    """Return the threshold this score failed, or None when it clears both.

    `fit >= 8 AND compounding >= 3` by default, and both numbers live in
    `profile.yaml` because both will be wrong on the first pass. These reason
    strings are how they get corrected with evidence instead of vibes.
    """
    volume = profile.get("volume") or {}
    # Fit first: a posting that fails both is a fit problem, and calling it a
    # compounding problem would fill the digest's "right job, wrong rung"
    # heading with postings that were never the right job.
    if score.fit < volume.get("fit_threshold", 8):
        return "below_fit_threshold"
    if score.compounding < volume.get("compounding_threshold", 3):
        return "below_compounding_threshold"
    return None


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

def sort_key(ranked: Ranked) -> tuple[int, int, int, float, str, str]:
    """Best first, and a total order.

    Two postings sharing a pair of numbers happens constantly -- there are only
    50 possible pairs and a night can bring 40 survivors. Without a TOTAL order
    the queue reshuffles between runs, yesterday's digest stops reproducing, and
    the thresholds become impossible to tune. Tier breaks the tie, then the
    newest posting, then the title and company alphabetically -- the last two
    carry no meaning and exist only to make the order deterministic, which is
    why they are in here at all.
    """
    posted = ranked.job.posted_at
    when = posted.timestamp() if isinstance(posted, datetime) else 0.0
    return (
        -ranked.score.fit,
        -ranked.score.compounding,
        ranked.tier or 99,
        -when,
        ranked.job.title.casefold(),
        ranked.job.company.casefold(),
    )


def plan_queue(
    scored: list[tuple[Job, Score]],
    profile: dict[str, Any],
    *,
    day: date,
    queued_today: int = 0,
    queued_this_week: int = 0,
) -> QueuePlan:
    """Decide what to queue tonight.

    The caps arrive as counts rather than being read here, because this module
    does no I/O -- the caller reads `applications` and passes the numbers in.
    The weekly cap is the binding one in practice: five a weekday is 25 a week
    exactly, so any weekend or catch-up activity spends against it.
    """
    volume = profile.get("volume") or {}
    daily_cap = volume.get("max_queued_per_weekday", 5)
    weekly_cap = volume.get("max_queued_per_week", 25)

    ranked = [
        Ranked(job=job, score=score, outcome="skipped", tier=title_tier(job.title, profile))
        for job, score in scored
    ]

    for item in ranked:
        item.reason = gate(item.score, profile)
        item.outcome = "skipped" if item.reason else "queued"

    daily_remaining = max(daily_cap - queued_today, 0)
    weekly_remaining = max(weekly_cap - queued_this_week, 0)
    slots = min(daily_remaining, weekly_remaining)

    eligible = sorted((r for r in ranked if r.outcome == "queued"), key=sort_key)
    cap_reason = (
        "weekly_cap_reached" if weekly_remaining <= daily_remaining else "daily_cap_reached"
    )
    for item in eligible[slots:]:
        item.outcome = "deferred"
        item.reason = cap_reason

    spent = len(eligible[:slots])
    ranked.sort(key=sort_key)
    return QueuePlan(
        ranked=ranked,
        day=day,
        daily_remaining=daily_remaining - spent,
        weekly_remaining=weekly_remaining - spent,
    )


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------

def summary_line(plan: QueuePlan, *, held: int = 0) -> str:
    """The one line that has to survive being read on a lock screen."""
    parts = []
    if plan.queued:
        parts.append(f"{len(plan.queued)} queued")
    if plan.deferred:
        parts.append(f"{len(plan.deferred)} deferred")
    if held:
        parts.append(f"{held} held")
    return " · ".join(parts) if parts else "nothing queued"


def render_digest(plan: QueuePlan, *, held: list[tuple[Job, str]] | None = None) -> str:
    """The morning email, as plain text.

    Ordered by what needs a decision: the queue, then the rejections most likely
    to be overturned, then what a cap postponed, then recruiter holds. The
    ordinary rejections are a count, because a list of two hundred no's is not
    information.
    """
    held = held or []
    lines = [f"{plan.day.isoformat()} — {summary_line(plan, held=len(held))}", ""]

    if plan.queued:
        lines.append(f"QUEUED ({len(plan.queued)})")
        for item in plan.queued:
            lines.extend(_entry(item))
        lines.append("")

    tempting = [r for r in plan.skipped if r.tempting]
    if tempting:
        lines.append(f"SKIPPED — right job, wrong rung ({len(tempting)})")
        lines.append("  These score well on fit. They do not leave the resume stronger.")
        for item in tempting:
            lines.extend(_entry(item))
        lines.append("")

    if plan.deferred:
        lines.append(f"DEFERRED — out of slots ({len(plan.deferred)})")
        lines.append("  Still live, and first in line on the next run.")
        for item in plan.deferred:
            lines.extend(_entry(item))
        lines.append("")

    if held:
        lines.append(f"HELD — an agency owns this client ({len(held)})")
        lines.append("  Applying direct can disqualify you. Call the recruiter instead.")
        for job, reason in held:
            lines.append(f"  · {job.title} — {job.company}")
            lines.append(f"    {reason}")
        lines.append("")

    other = [r for r in plan.skipped if not r.tempting]
    if other:
        counts: dict[str, int] = {}
        for item in other:
            reason = item.reason or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        tally = ", ".join(f"{reason} {count}" for reason, count in sorted(counts.items()))
        lines.append(f"Also scored and skipped: {len(other)} ({tally})")

    lines.append(
        f"Slots left: {plan.daily_remaining} today, {plan.weekly_remaining} this week."
    )
    return "\n".join(lines).rstrip() + "\n"


def _entry(item: Ranked) -> list[str]:
    tier = f" · tier {item.tier}" if item.tier else ""
    place = item.job.location_raw or item.job.region or "location not stated"
    lines = [
        f"  · {item.job.title} — {item.job.company}",
        f"    fit {item.score.fit}/10 · compounding "
        f"{item.score.compounding}/5{tier} · {place}",
        f"    {item.score.verdict}",
    ]
    if item.score.builds:
        lines.append(f"    builds: {item.score.builds}")
    if item.score.concerns:
        lines.append(f"    concerns: {item.score.concerns}")
    lines.extend(f"    flag: {flag}" for flag in item.score.soft_flags)
    lines.append(f"    {item.job.apply_url}")
    return lines


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
Score this job posting for the candidate below. Return JSON only.

CANDIDATE
{summary}

TARGET TITLES (tier 1 is the strongest match; an unlisted title may still score well)
{titles}

POSTING
Company: {company}
Title: {title}
Location: {location} (classified: {region})
Employment type: {employment_type}
Stated salary: {salary}
Description:
{description}

SCORE ON TWO AXES
fit (1-10): right family of role, open to this experience level, real overlap
  with the candidate's actual skills. A posting asking for a decade of
  experience is a low fit however well the subject matter matches.
compounding (1-5): does a year in this seat leave the resume materially
  stronger? Look for a named tool, a portfolio-able artifact, a paid
  credential, or a title that reads as a step up. A role that pays the bills
  and teaches nothing is a 1, however pleasant.

Judge the posting as written. Do not assume unstated seniority, do not assume
remote, and do not give credit for a tool the description does not name.

Return exactly:
{{"fit": int, "compounding": int, "title_bucket": str, "verdict": str,
  "builds": str, "concerns": str, "soft_flags": [str]}}

verdict: one sentence, read by a person deciding whether to spend a slot.
builds: what a year here adds, or "" if nothing.
concerns: what would make this a bad use of a slot, or "".
soft_flags: short warnings worth reading before applying, e.g. quota language.
"""


def scoring_prompt(
    job: Job,
    profile: dict[str, Any],
    *,
    bullets: dict[str, Any] | None = None,
    description_limit: int = 6000,
) -> str:
    """Render the prompt for one posting.

    The description is truncated, not summarized. A posting's first few thousand
    characters carry the requirements; what follows is benefits boilerplate and
    an EEO statement, and paying to read those on every posting every night buys
    no signal.

    The candidate summary comes from `bullets.yaml` -- the same text the resume
    claims, rather than a second description of the same person that can drift
    from it. Pass `bullets` explicitly to score against something else; the
    default reads the committed file, which is the only I/O in this module.
    """
    titles = [
        f"{key}: " + ", ".join(str(name) for name in listed)
        for key, listed in sorted((profile.get("titles") or {}).items())
        if key.startswith("tier_") and listed
    ]

    description = job.description or "(no description provided)"
    if len(description) > description_limit:
        description = description[:description_limit] + "\n[truncated]"

    salary = "not stated"
    if job.salary_min or job.salary_max:
        low = f"{job.salary_min:,}" if job.salary_min else "?"
        high = f"{job.salary_max:,}" if job.salary_max else "?"
        salary = f"{low}-{high}"

    summary = (bullets if bullets is not None else load_bullets()).get(
        "summary_base"
    ) or "(no candidate summary loaded)"

    return PROMPT_TEMPLATE.format(
        summary=" ".join(str(summary).split()),
        titles="\n".join(titles) or "(none configured)",
        company=job.company,
        title=job.title,
        location=job.location_raw or "not stated",
        region=job.region or "unclassified",
        employment_type=job.employment_type or "not stated",
        salary=salary,
        description=description,
    )
