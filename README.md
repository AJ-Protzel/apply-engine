# apply-engine

A job-application pipeline that runs on hosted infrastructure, sources postings from public ATS APIs, filters them against an explicit ruleset, ranks what survives, and stops at the submit button.

Job search at volume fails for a boring reason: the filtering happens in a browser tab at 11pm, inconsistently, by a person who is tired. This moves the filtering into code where it is deterministic, logged, and measurable — and leaves the judgment call that actually matters, whether to apply, to a human.

---

## The problem it solves

Roughly **3% of "data engineer" postings are entry-level** (219 of 6,877 sampled, May 2026). "Analytics engineer" is closer to **8%** — nearly triple, for a substantially overlapping skill set. Applying harder to the first title does not fix that. Applying to the right set of titles does.

So the ranking layer scores against a defined profile rather than a job title, and the filter layer records *why* it rejected every posting it rejected. After a week, the kill-rule log says which rules are over-firing. That turns "my filters are probably too strict" into a number.

---

## Architecture

Two hosted runtimes, split by what each is good at. Neither needs a laptop to be on.

```
                    ┌──────────────────────────────┐
  04:30 PT daily    │  GitHub Actions              │  unrestricted egress
  ────────────────► │  ingest → normalize →        │  ──────────────────────►
                    │  dedupe → filter → insert    │           Postgres
                    └──────────────────────────────┘          (Supabase)
                                                                  ▲   │
                    ┌──────────────────────────────┐              │   │
  06:45 PT weekdays │  Scheduled LLM session       │  reads/writes│   │
  ────────────────► │  score → tailor → queue      │ ─────────────┘   │
                    │  → email digest              │ ◄────────────────┘
                    └──────────────────────────────┘
                                  │
                                  ▼
                        "5 queued · 1 held · [open queue]"
```

**Why the split.** Ingest needs raw outbound network to hit nine different APIs, which is what Actions is good at and free for on public repos. Scoring and tailoring need judgment, and reach Postgres and Gmail over tooling rather than raw sockets. Each half fails independently: if ingest breaks, yesterday's postings still get scored; if scoring breaks, ingest keeps accumulating. A watchdog job checks that ingest reported success and alerts when it did not.

---

## Sources

Nine, all public JSON endpoints or free-key APIs. No scraping, no headless browser, no paid data.

| Source | Auth | Shape |
|---|---|---|
| Greenhouse | none | per-company board |
| Ashby | none | per-company board, includes compensation |
| Lever | none | per-company board |
| Workable | none | per-company board |
| Recruitee | none | per-company board |
| Remotive | none | board-wide feed |
| RemoteOK | none | board-wide feed |
| USAJOBS | free key | federal, unlimited |
| Adzuna | free key | aggregator, 1,000 calls/month |

The seven keyless sources are implemented. USAJOBS and Adzuna are specified and deliberately not built yet — they gate on signups, and there was no reason to hold the first working ingest for them.

Per-source behaviour: three retries with exponential backoff, 10s timeout, one request per second across the entire run. A 404 on a company slug is treated as a wrong slug rather than an error — the company's failure count increments and it deactivates after five consecutive misses, so dead boards fall out of rotation instead of failing quietly forever.

---

## The filter layer

`filters.py` is pure functions and no I/O, which is why it can be tested exhaustively. Two design decisions in it are worth calling out, because both are mistakes I made first and then fixed:

**The allow-override runs before the kill rules, not after.** "Sales Operations Analyst" and "Salesforce Administrator" both contain a token the sales kill rule matches, and both are real non-quota roles. Implemented as kill-then-rescue, an entire title family disappears silently. Implemented as override-then-kill, it works. There is a test asserting the ordering, because this is the kind of thing that gets refactored back into a bug.

**The recruiter-conflict check does not fuzzy-match.** If a staffing agency has submitted you to an employer, applying directly typically disqualifies you outright. So the pipeline holds those jobs — but matching on approximate company names would silently hide good postings, and *nothing in the system would ever surface that it happened*. Normalized equality and domain suffix only. A false negative gets caught by a human reading the digest; a false positive is invisible. When the two error modes are asymmetric, the filter should fail toward the visible one.

Every rejection writes the rule that caused it to `job_filters.kill_rule`. The years-of-experience rule is knowingly over-broad — it cannot distinguish "5+ years required" from "5 years of combined experience preferred" — and there is a test asserting that current wrong behaviour, so that loosening it later is a deliberate, visible change rather than a drift.

---

## Ranking

Surviving postings are scored on two axes, and need both to enter the queue:

- **fit** (1–10) — right family of role, open to the experience level, real overlap with actual skills.
- **compounding** (1–5) — does a year in this seat leave the résumé materially stronger? A named tool, a portfolio artifact, a paid credential, or a title that reads as a step up.

`fit >= 8 AND compounding >= 3`. A job scoring 10 on fit and 1 on compounding does not queue, and gets named in the digest as explicitly skipped, because those are exactly the ones that are tempting in the moment.

Both thresholds are config values in `profile.yaml`. They will be wrong on the first pass; the kill-rule log is how they get corrected with evidence rather than vibes.

---

## Why it never submits

This is a product decision, not a missing feature.

No ATS accepts applications over a public API — Greenhouse, Lever, Ashby, Workable and Recruitee all publish postings without a key and none accept a submission without employer credentials. So the last mile is always a browser, and automating it means driving a form with no error handling worth the name.

The failure mode is quiet and expensive: a mis-parsed dropdown submits "0 years experience" to a role you'd have gotten, a screening question gets answered wrong, and there is no recall. Meanwhile the marginal value of automating the final click is about fifteen seconds per application.

Automating the ninety minutes of searching and filtering is worth it. Automating the fifteen seconds of judgment is not. The system prepares everything up to the submit button, and a person clicks it.

The same reasoning rules out LinkedIn, Handshake, and Indeed automation entirely — those are ToS violations with account-loss risk attached, on the lowest-quality application channel available.

---

## Layout

```
config/
  profile.yaml         filters, thresholds, geography, title tiers  (public)
  identity.example.yaml  template; the real file is gitignored
  companies.yaml       employers + validated ATS slugs
  bullets.yaml         tagged résumé bullet bank
sql/001_init.sql       schema
src/apply_engine/
  sources/             one module per source, all returning RawJob
  normalize.py         RawJob -> Job, region/type classification, dedupe
  filters.py           hard rules, pure functions
  db.py                Supabase client, idempotent upserts
  find_slug.py         guess and confirm a company's ATS slug
  run_ingest.py        entrypoint
tests/test_filters.py
```

Personal details — address, phone, EEO answers — live in `config/identity.yaml`, which is gitignored. This repo is public, so the config that drives decisions is committed and the config that identifies a person is not.

---

## Running it

```bash
pip install -e ".[dev]"
pytest -q
```

Dry run against the board-wide feeds, no database and no credentials needed:

```bash
python -m apply_engine.run_ingest --dry-run
```

Confirm a company's ATS slug before adding it to `companies.yaml`:

```bash
python -m apply_engine.find_slug "Blue Shield of California"
```

Full ingest needs `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, which in Actions come from repository secrets.

---

## Status

Working, and verified against live endpoints: the schema, seven keyless connectors, normalization with cross-source dedupe, the filter layer (49 tests), the ingest entrypoint with run telemetry, and the Actions workflow.

Connector confidence, stated honestly because "returns an empty list" and "is broken" look identical from the outside:

| Connector | Verified against live data |
|---|---|
| Greenhouse | yes — four real boards, 130–830 postings each |
| Lever | yes — two real boards |
| Ashby | yes — two real boards |
| Workable | response shape confirmed correct; no populated board in the sample yet |
| Recruitee | not yet — no valid slug tried so far |
| Remotive, RemoteOK | yes — 117 postings through the full pipeline |

Next: grow `companies.yaml` to 150+ validated slugs, then wire the scoring and tailoring pass, which runs on a schedule configured outside this repo.

MIT licensed.
