"""Shared shapes.

`RawJob` is what a source connector returns: whatever that API gave us, lightly
coerced. `Job` is the normalized row that lands in Postgres. Every connector
produces `RawJob`; only `normalize.py` produces `Job`. Keeping those separate is
what lets a new source be added without touching the filter or storage layers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# The vocabulary. This is the only place these strings are defined.
#
# They previously lived in four places -- a comment here, a comment in
# 001_init.sql, the branches of normalize.classify_employment_type, and the
# allow/deny lists in profile.yaml -- and two of the four had drifted. The
# schema said `c2h` while the code emitted `contract_to_hire`, and profile.yaml
# denied `internship` for a value the code spells `intern`, so the deny rule
# never fired and internships were killed by the allow-list instead, logging a
# kill_rule that named the wrong reason. A tuning log is only worth having if it
# is right.
#
# Now: Literal here, a CHECK constraint in 001_init.sql generated from the same
# list, and a test asserting profile.yaml only names values that appear here.
# ---------------------------------------------------------------------------

EmploymentType = Literal["full_time", "contract", "contract_to_hire", "part_time", "intern"]
Region = Literal["remote-us", "ca-norcal", "ca-other", "wa", "other"]

EMPLOYMENT_TYPES: frozenset[str] = frozenset(get_args(EmploymentType))
REGIONS: frozenset[str] = frozenset(get_args(Region))


class RawJob(BaseModel):
    """One posting as the source described it."""

    source: str
    source_job_id: str
    company: str
    title: str
    location_raw: str | None = None
    description: str | None = None
    apply_url: str
    posted_at: datetime | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    employment_type_raw: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    """One posting, normalized. Mirrors the `jobs` table."""

    source: str
    source_job_id: str
    company: str
    title: str
    location_raw: str | None = None
    region: Region | None = None
    employment_type: EmploymentType | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    description: str | None = None
    apply_url: str
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        """Cross-source identity.

        The same posting appears on Greenhouse and on an aggregator with
        different ids. `(source, source_job_id)` will not catch that; this will.
        """
        return (
            self.company.strip().casefold(),
            self.title.strip().casefold(),
            (self.location_raw or "").strip().casefold(),
        )


class FilterResult(BaseModel):
    """Why a job did or did not survive the hard filters."""

    passed: bool
    kill_rule: str | None = None

    @classmethod
    def kill(cls, rule: str) -> FilterResult:
        return cls(passed=False, kill_rule=rule)

    @classmethod
    def keep(cls) -> FilterResult:
        return cls(passed=True)
