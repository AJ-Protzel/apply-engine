"""Workable job boards.

GET https://www.workable.com/api/accounts/{slug}?details=true

Heavy in mid-size and non-tech employers, which is most of the Sacramento
region. No auth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import RawJob
from .base import get_json

SOURCE = "workable"
BASE = "https://www.workable.com/api/accounts/{slug}"


def fetch(slug: str, company_name: str | None = None) -> list[RawJob]:
    payload = get_json(BASE.format(slug=slug), params={"details": "true"})
    entries = payload.get("jobs", []) if isinstance(payload, dict) else []
    resolved = company_name or (payload.get("name") if isinstance(payload, dict) else None)
    return [
        job
        for entry in entries
        if (job := _parse(entry, slug, resolved)) is not None
    ]


def _parse(entry: dict[str, Any], slug: str, company_name: str | None) -> RawJob | None:
    job_id = entry.get("shortcode") or entry.get("id")
    title = entry.get("title")
    url = entry.get("url") or entry.get("application_url")
    if not (job_id and title and url):
        return None

    return RawJob(
        source=SOURCE,
        source_job_id=str(job_id),
        company=company_name or slug,
        title=title,
        location_raw=_location(entry),
        description=entry.get("description"),
        apply_url=url,
        posted_at=_parse_date(entry.get("published_on") or entry.get("created_at")),
        employment_type_raw=entry.get("employment_type"),
        raw=entry,
    )


def _location(entry: dict[str, Any]) -> str | None:
    """Workable spreads the place across three shapes, and `location` is not one.

    A posting from `?details=true` carries the place at the TOP level as
    `city`/`state`/`country`, plus a `locations[]` list keyed
    `city`/`region`/`country`. There is no `location` key at all. Reading only
    `location` -- which an earlier version did -- returned None for every
    Workable posting, and a posting with no location sails through
    `geography_kill` untouched, because that rule deliberately keeps what it
    cannot read. A London claims job surviving as a plausible Sacramento one is
    exactly what the fixture suite exists to catch.
    """
    if entry.get("telecommuting"):
        return "Remote"

    location = entry.get("location")
    if isinstance(location, str) and location.strip():
        return location.strip()
    if isinstance(location, dict):
        if joined := _join(location.get("city"), location.get("region"),
                           location.get("country")):
            return joined

    if joined := _join(entry.get("city"), entry.get("state"), entry.get("country")):
        return joined

    for candidate in entry.get("locations") or []:
        if not isinstance(candidate, dict) or candidate.get("hidden"):
            continue
        if joined := _join(candidate.get("city"), candidate.get("region"),
                           candidate.get("country")):
            return joined
    return None


def _join(*parts: Any) -> str | None:
    joined = ", ".join(str(part).strip() for part in parts if part and str(part).strip())
    return joined or None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
