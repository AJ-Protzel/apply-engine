# apply-engine

A job-application pipeline that runs on hosted infrastructure, sources postings from public ATS APIs, filters them against an explicit ruleset, ranks what survives, and stops at the submit button.

> **Archived, finished, and no longer running.** It ran nightly from 2026-08-27 to 2026-09-01 — six scheduled runs, three of them clean — and then the Supabase project behind it went away and the last two failed on connection. The schedule is now removed rather than left to fail every morning. What is here is the complete ingest-and-rank engine with its tests; the parts that never ran are named as such below rather than described in the future tense. `python -m apply_engine.run_ingest --dry-run` still works on a fresh clone against live endpoints, with no credentials and no database.

Job search at volume fails for a boring reason: the filtering happens in a browser tab at 11pm, inconsistently, by a person who is tired. This moves the filtering into code where it is deterministic, logged, and measurable — and leaves the judgment call that actually matters, whether to apply, to a human.

---

## The problem it solves

Roughly **3% of "data engineer" postings are entry-level** (219 of 6,877 sampled, May 2026). "Analytics engineer" is closer to **8%** — nearly triple, for a substantially overlapping skill set. Applying harder to the first title does not fix that. Applying to the right set of titles does.

So the ranking layer scores against a defined profile rather than a job title, and the filter layer records *why* it rejected every posting it rejected. After a week, the kill-rule log says which rules are over-firing. That turns "my filters are probably too strict" into a number.

Here is that number from the last clean production run, 2026-08-30: **10,442 postings pulled, 184 dropped as cross-source duplicates, 402 survivors, 9,856 kills.** The hard filters cut 96% of what reached them, and the log says exactly where:

| Kills | Rule |
|---:|---|
| 6,117 | `title:` senior, sr, staff, principal, lead, manager, director, head of, architect, VP, chief |
| 1,487 | `geography:outside_allowed_region:other` |
| 1,053 | `title:` sales, account executive, account manager, business development, BDR, SDR |
| 818 | `description:` 5–19 years of experience |
| 135 | `geography:deny_metro:Long Beach` |
| 119 | `description:` sales quota, carry a quota, on-target earnings, OTE, book of business |
| 37 | `title:` intern, internship |
| 32 | `description:` security clearance, TS/SCI, polygraph, public trust required |
| 18 | `description:` cold call, prospecting, pipeline generation, close deals, hit your number |
| 17 | `geography:deny_state:OR` |
| 8 | `employment_type:part_time` |
| 7 | `geography:deny_metro:Los Angeles` |
| 7 | four food-service and retail-floor title rules |
| 1 | `employment_type:not_allowed:intern` |

One rule accounts for 62% of all rejections and two account for 77%. That is not something you can guess at, and it is the difference between tuning a filter and redecorating one.

The tally also caught a bug. That last row, `employment_type:not_allowed:intern`, should not be there — there is an explicit rule for denying internships and it should have fired instead. `profile.yaml` spelled the value `internship` where the code emits `intern`, so the deny rule never matched anything and the allow-list swept those postings up instead. The pipeline reached the right verdict for the wrong recorded reason, every night it ran. A log that records *which* rule fired is what makes a one-row discrepancy like that legible at all.

The same tally is reproducible on a fresh clone with no credentials: `--dry-run` prints it for the two board-wide feeds (115 postings, 21 survivors, as of 2026-09-12).

---

## Architecture

Two hosted runtimes, split by what each is good at. Neither needs a laptop to be on.

```
                    ┌──────────────────────────────┐
  04:30 PT daily    │  GitHub Actions              │  unrestricted egress
  ────────────────► │  ingest → normalize →        │  ──────────────────────►
      (ran daily)   │  dedupe → filter → insert    │           Postgres
                    └──────────────────────────────┘          (Supabase)
                                                                  ▲   │
                    ┌──────────────────────────────┐              │   │
  06:45 PT weekdays │  Scheduled LLM session       │  reads/writes│   │
  ────────────────► │  score → tailor → queue      │ ─────────────┘   │
   (never wired up) │  → email digest              │ ◄────────────────┘
                    └──────────────────────────────┘
                                  │
                                  ▼
                        "5 queued · 1 deferred · [open queue]"
```

**Why the split.** Ingest needs raw outbound network to hit seven different APIs, which is what Actions is good at and free for on public repos. Scoring and tailoring need judgment, and reach Postgres and Gmail over tooling rather than raw sockets. Each half fails independently: if ingest breaks, yesterday's postings still get scored; if scoring breaks, ingest keeps accumulating.

**What actually ran.** The top half, nightly, for six days — 2026-08-27 through 09-01, three of the six runs clean. The bottom half never got its scheduled session. What exists in this repo instead is `scoring.py` — the deterministic half of that runtime: the gate, the caps, the ordering, the digest and the prompt, with the model call left as a boundary for a caller to fill. That division is the subject of [Ranking](#ranking), and it is why the ranking policy carries 43 tests despite never having scored a real posting.

---

## Sources

Nine were specified. Seven are built — all public JSON endpoints, no scraping, no headless browser, no paid data.

| Source | Auth | Shape | Built |
|---|---|---|---|
| Greenhouse | none | per-company board | yes |
| Ashby | none | per-company board, includes compensation | yes |
| Lever | none | per-company board | yes |
| Workable | none | per-company board | yes |
| Recruitee | none | per-company board | yes |
| Remotive | none | board-wide feed | yes |
| RemoteOK | none | board-wide feed | yes |
| USAJOBS | free key | federal, unlimited | no |
| Adzuna | free key | aggregator, 1,000 calls/month | no |

USAJOBS and Adzuna gate on signups, and there was no reason to hold the first working ingest for them. They were never built and, with the project closed, will not be — the work is one more module in `sources/` returning `RawJob`, which is exactly the interface that makes that claim cheap to believe.

Per-source behaviour: three retries with exponential backoff, 10s timeout, one request per second across the entire run. A 404 on a company slug is treated as a wrong slug rather than an error — the company's failure count increments and it deactivates after five consecutive misses, so dead boards fall out of rotation instead of failing quietly forever.

`companies.yaml` holds 150 employer boards, every slug confirmed against a live endpoint — 80 Greenhouse, 52 Ashby, 17 Lever, 1 Recruitee, and no Workable at all. That last number is why the Workable bug in [What the fixture suite found](#what-the-fixture-suite-found) was latent rather than live, and it is the honest outcome of the search: the Sacramento-region employers the Workable connector was written for — Kaiser, Sutter, Dignity, UC Davis Health, SMUD, the County and the City — all run Workday or a bespoke portal, and none of them exposes a keyless public board. The connector works; the region just does not use it.

The file is also the seed: `db.sync_companies()` inserts the boards the table does not have yet, and *only* those. The file owns membership; the table owns runtime state. An upsert there instead of an insert would set `active` back to true every night and quietly undo every deactivation the failure counter had earned.

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

`fit >= 8 AND compounding >= 3`, both thresholds config values in `profile.yaml`, because both will be wrong on the first pass.

**The model produces two numbers and a sentence. Everything done with them is ordinary code.** That is the whole design of `scoring.py`: `scoring_prompt()` renders the prompt, `Score` validates what comes back, and the judgment sits in the gap between them, owned by whatever runtime makes the API call. The gate, the daily and weekly caps, the ordering and the digest are pure functions on the other side of that seam — so the policy that actually needs tuning is testable at zero cost, and the part that costs money per posting is small enough to read in one sitting.

Three things in that policy were wrong the first time, and each is a test now:

**Losing to a cap is not the same as being rejected.** Both leave a job un-queued tonight, so the first version recorded both as `skipped`. But a job that scored 9 and came eleventh on a five-slot night is still a 9 tomorrow, and marking it skipped removed it from consideration forever. Cap overflow is `deferred` — no `applications` row is written at all, so it competes again on the next run.

**A posting that fails both thresholds is recorded against fit, not compounding.** The digest gives high-fit/low-compounding rejections their own heading — "right job, wrong rung" — because that is the rejection a tired person overturns by hand at 11pm, and it deserves to be argued with rather than buried in a count. The first version named the compounding failure whenever it occurred, and the heading promptly filled with line-cook postings that had scored 1 on fit. A heading is only worth reading if everything under it belongs there.

**The queue order has to be total.** There are 50 possible score pairs and a night can bring 40 survivors, so ties are constant. Ordering on the scores alone leaves the rest to input order: the queue reshuffles between runs, yesterday's digest stops reproducing, and the thresholds become impossible to tune. Tier breaks the tie, then recency, then two fields that carry no meaning at all and exist purely to make the sort deterministic.

---

## Why it never submits

This is a product decision, not a missing feature.

No ATS accepts applications over a public API — Greenhouse, Lever, Ashby, Workable and Recruitee all publish postings without a key and none accept a submission without employer credentials. So the last mile is always a browser, and automating it means driving a form with no error handling worth the name.

The failure mode is quiet and expensive: a mis-parsed dropdown submits "0 years experience" to a role you'd have gotten, a screening question gets answered wrong, and there is no recall. Meanwhile the marginal value of automating the final click is about fifteen seconds per application.

Automating the ninety minutes of searching and filtering is worth it. Automating the fifteen seconds of judgment is not. The system prepares everything up to the submit button, and a person clicks it.

The same reasoning rules out LinkedIn, Handshake, and Indeed automation entirely — those are ToS violations with account-loss risk attached, on the lowest-quality application channel available.

---

## What the fixture suite found

The connector tests replay seven real API responses, captured once and committed under `tests/fixtures/`. They exist because "returns an empty list" and "is broken" look identical from the outside — and writing them turned up two connectors that had been wrong the entire time the pipeline was live:

**Workable returned `location_raw = None` for every posting it parsed.** The payload has no `location` key at all; the place sits at the top level as `city`/`state`/`country`, plus a `locations[]` array keyed differently again. And a posting with no location is *kept* by `geography_kill`, deliberately, because killing on unreadable data hides good jobs. So a UK posting would have passed the geography filter as readily as a Sacramento one.

Would have: this one was latent, not live. `companies.yaml` never contained a single Workable slug — the Sacramento-region employers the connector was written for turned out to run Workday or bespoke portals, and the production run stats have no `raw_workable` line at all. So the bug was waiting for the first Workable employer to be added, and it would have mis-filed every posting on that board from the day it arrived. There is now a test asserting a London claims-handler job is killed for being outside the allowed regions, which is what makes the fix load-bearing rather than cosmetic.

**Recruitee returned `posted_at = None` for every posting.** It stamps dates as `2026-07-29 08:20:35 UTC`, and `fromisoformat` rejects the trailing zone name. This one *was* live — 4 postings a night, arriving undated — and a null date is indistinguishable from a source that publishes no date, which is why nothing surfaced it.

Neither would have been caught by reading the code more carefully, and neither showed up as an error in a run. Both needed a recorded payload and an assertion about a specific field. That is the argument for fixtures over mocks in one line: a mock asserts what you believed the API returns, and both of these were failures of belief.

The third one is the `intern` / `internship` mismatch described [at the top](#the-problem-it-solves): a denied value that no code could ever emit, so the rule never fired and the kill log recorded the wrong reason. The vocabulary now has exactly one owner in `models.EmploymentType`, a CHECK constraint in the schema built from the same list, and a test asserting the config only names values the code can actually emit — which is the general version of all three bugs. Two systems holding the same fact will drift, and the only question is whether anything notices.

---

## Layout

```
config/
  profile.yaml         filters, thresholds, geography, title tiers  (public)
  identity.example.yaml  template; the real file is gitignored
  companies.yaml       150 employers + validated ATS slugs
  bullets.yaml         tagged résumé bullet bank
sql/001_init.sql       the whole schema; there is no 002
src/apply_engine/
  sources/             one module per source, all returning RawJob
  normalize.py         RawJob -> Job, region/type classification, dedupe
  filters.py           hard rules, pure functions
  scoring.py           gate, caps, ordering, digest, prompt; no model call
  db.py                Supabase client, idempotent upserts, company seeding
  find_slug.py         guess and confirm a company's ATS slug
  run_ingest.py        entrypoint
tests/
  test_filters.py      49 tests — hard rules, against the real profile.yaml
  test_normalize.py    56 — region, employment type, dedupe, drift guards
  test_scoring.py      43 — gate, caps, ordering, digest, prompt
  test_sources.py      16 — all seven parsers, offline, on real payloads
  fixtures/            seven recorded API responses
```

Personal details — address, phone, EEO answers — live in `config/identity.yaml`, which is gitignored. This repo is public, so the config that drives decisions is committed and the config that identifies a person is not.

---

## Running it

```bash
pip install -e ".[dev]"
pytest -q
```

164 tests, about a third of a second, no network and no credentials.

Dry run against the board-wide feeds — live endpoints, no database, no keys. This is the one command that does something real on a fresh clone, and it prints the kill tally shown at the top of this README:

```bash
python -m apply_engine.run_ingest --dry-run
```

Confirm a company's ATS slug before adding it to `companies.yaml`. A slug that looks right and belongs to someone else is the expensive failure here: `lever/blue` answers with ten live postings and is BlueCloud Services, not Blue Shield of California. `find_slug` checks every hit against the name the board itself reports and prints MISMATCH rather than CONFIRMED:

```bash
python -m apply_engine.find_slug "Blue Shield of California"
```

A full ingest needs `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` against a database with `sql/001_init.sql` applied. In Actions those came from repository secrets; the workflow is now dispatch-only.

---

## Status

Complete and closed. What is here works and is tested:

| Piece | State |
|---|---|
| Schema | `sql/001_init.sql` — 8 tables, 2 views, CHECK-constrained vocabularies |
| Connectors | 7 of 9, each verified against a recorded live payload |
| Normalization | region and employment-type classification, cross-source dedupe |
| Filters | hard rules as pure functions, every rejection logged with its cause |
| Ranking | gate, daily and weekly caps, total ordering, digest, prompt |
| Company list | 150 boards, every slug confirmed against a live endpoint |
| Telemetry | a `runs` row on every exit path, including a crash |
| Tests | 164, offline, wired to run on every push |

What is *not* here, stated plainly: the USAJOBS and Adzuna connectors were never built; the scheduled session that would call a model, tailor a résumé and send the digest was never wired up, so no posting has ever actually been scored; and the tailoring step exists only as the bullet bank and the rules in `bullets.yaml` that would have governed it.

The most useful thing the project taught me is in [What the fixture suite found](#what-the-fixture-suite-found). Three separate bugs came down to one shape: a value that two parts of the system spelled differently, or a field one side expected where the other never put it. All three were invisible from the outside — no exception, no error, every run reporting success — and all three were found by writing down what the other side actually sends and asserting against it. That is why the kill-rule log exists, and why it is the feature I would build first again.

MIT licensed.
