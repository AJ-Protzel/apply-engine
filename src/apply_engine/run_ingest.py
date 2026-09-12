"""Ingest entrypoint. Runs on GitHub Actions at 04:30 PT daily.

Pipeline: seed companies from companies.yaml -> fetch every source ->
normalize -> dedupe across sources -> upsert -> run hard filters -> check
recruiter conflicts -> write telemetry.

No scoring and no tailoring happen here. Those need judgment and run in the
scheduled session at 06:45 PT, which reads what this wrote.

This process must write a `runs` row on every exit path, including a crash. The
watchdog task at 09:00 PT alerts on its absence.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections import Counter
from typing import Any

from . import db, filters, normalize
from .config import load_profile
from .models import Job
from .sources import ATS_MODULES, BOARD_MODULES, REMOTE_ONLY_SOURCES
from .sources.base import BoardNotFound, SourceUnavailable

log = logging.getLogger("apply_engine.ingest")


def collect(db_client: Any, *, dry_run: bool = False) -> tuple[list[Job], Counter]:
    """Fetch every configured source. One bad company never kills the run."""
    stats: Counter = Counter()
    raw_jobs = []

    if dry_run:
        companies = []
    else:
        # companies.yaml is the reviewed list; the table is the runtime copy. A
        # fresh install has an empty table, so without this the run would poll
        # the two board-wide feeds and nothing else -- and report success while
        # doing it.
        stats["companies_added"] = db.sync_companies(db_client)
        companies = db.active_companies(db_client)
    stats["companies_polled"] = len(companies)

    for company in companies:
        module = ATS_MODULES.get(company["ats"])
        if module is None:
            log.warning("Unknown ATS %r for %s", company["ats"], company["name"])
            continue
        try:
            found = module.fetch(company["slug"], company["name"])
        except BoardNotFound:
            # Wrong slug, or they left that ATS. Expected, not an error.
            stats["company_not_found"] += 1
            if db.mark_company_failed(db_client, company):
                stats["company_deactivated"] += 1
            continue
        except SourceUnavailable as exc:
            log.warning("%s (%s) unavailable: %s", company["name"], company["ats"], exc)
            stats["company_unavailable"] += 1
            db.mark_company_failed(db_client, company)
            continue

        db.mark_company_ok(db_client, company["id"])
        stats[f"raw_{company['ats']}"] += len(found)
        raw_jobs.extend(found)

    for name, module in BOARD_MODULES.items():
        try:
            found = module.fetch()
        except (BoardNotFound, SourceUnavailable) as exc:
            log.warning("%s unavailable: %s", name, exc)
            stats[f"{name}_unavailable"] += 1
            continue
        stats[f"raw_{name}"] += len(found)
        raw_jobs.extend(found)

    stats["raw_total"] = len(raw_jobs)
    jobs = [
        normalize.normalize(raw, remote_hint=raw.source in REMOTE_ONLY_SOURCES)
        for raw in raw_jobs
    ]
    deduped = normalize.dedupe(jobs)
    stats["after_dedupe"] = len(deduped)
    stats["dropped_as_duplicate"] = len(jobs) - len(deduped)
    return deduped, stats


def apply_filters(
    db_client: Any, stored: list[dict[str, Any]], profile: dict[str, Any], stats: Counter
) -> None:
    """Hard filters, then the recruiter conflict guard.

    Order matters: recruiter conflict runs only on jobs that already passed the
    hard rules, so a conflict is never recorded for a job that was going to be
    killed anyway.
    """
    submissions = db.blocked_employers(db_client)
    results = {}
    conflicts = []

    for row in stored:
        job = Job(**{k: v for k, v in row.items() if k in Job.model_fields})
        result = filters.evaluate(job, profile)
        results[row["id"]] = result

        if result.passed:
            conflict = filters.recruiter_conflict(job, submissions)
            if conflict:
                conflicts.append((row["id"], job, conflict))
                stats["held_recruiter_conflict"] += 1
            else:
                stats["passed"] += 1
        else:
            stats["killed"] += 1
            stats[f"kill::{result.kill_rule}"] += 1

    db.write_filter_results(db_client, results)

    for job_id, job, conflict in conflicts:
        log.info("Held %s at %s -- %s owns this client",
                 job.title, job.company, conflict.get("agency"))
        db.skip_for_recruiter_conflict(db_client, job_id, conflict)


def dry_run(profile: dict[str, Any]) -> int:
    """The two board-wide feeds through the filters, printing the kill tally.

    No database, no credentials, no company list -- so this is the one command
    that works on a fresh clone, which makes it the demonstration of the claim
    the whole repo rests on: every rejection names the rule that caused it, so
    "my filters are probably too strict" becomes a number. The tally below is
    that number, on live postings, in about forty seconds.
    """
    jobs, stats = collect(None, dry_run=True)
    tally: Counter = Counter()
    kept = 0

    for job in jobs:
        result = filters.evaluate(job, profile)
        if result.passed:
            kept += 1
        else:
            tally[result.kill_rule] += 1

    log.info("Dry run: %d postings, %d pass the hard filters (%d killed)",
             len(jobs), kept, len(jobs) - kept)
    for rule, count in tally.most_common():
        log.info("  %4d  %s", count, rule)
    log.info("Source counts: %s",
             {k: v for k, v in sorted(stats.items()) if k.startswith("raw_")})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest job postings.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Board sources only, no database writes.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    profile = load_profile()

    if args.dry_run:
        return dry_run(profile)

    db_client = db.client()
    run_id = db.start_run(db_client, "ingest")

    try:
        jobs, stats = collect(db_client)
        stored = db.upsert_jobs(db_client, jobs)
        stats["upserted"] = len(stored)
        apply_filters(db_client, stored, profile, stats)
    except Exception:
        error = traceback.format_exc()
        log.error("Ingest failed:\n%s", error)
        db.finish_run(db_client, run_id, ok=False, error=error)
        return 1

    db.finish_run(db_client, run_id, ok=True, stats=dict(stats))
    log.info("Ingest complete: %s", dict(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
