-- ---------------------------------------------------------------------------
-- apply-engine schema, migration 001
--
-- Applied against the existing Supabase project. Every table is prefixed
-- nothing special -- it shares a project with an unrelated app, so names are
-- chosen to not collide.
--
-- Design notes worth knowing before changing anything:
--   * jobs is append-mostly. We never delete a posting we've seen; last_seen_at
--     tracks whether it is still live.
--   * job_filters.kill_rule is the tuning mechanism. Every rejection records
--     WHICH rule rejected it, so false-positive rates are measurable instead of
--     anecdotal. Do not "clean up" by dropping rejected rows.
--   * job_scores keeps history for jobs that never queued. Same reason.
-- ---------------------------------------------------------------------------

-- companies we poll
create table if not exists companies (
  id          bigserial primary key,
  name        text not null,
  ats         text not null check (ats in ('greenhouse','lever','ashby','workable','recruitee')),
  slug        text not null,
  tier        int  not null default 2,      -- 1 = check first, 3 = long tail
  active      bool not null default true,
  last_ok_at  timestamptz,
  fail_count  int  not null default 0,
  unique (ats, slug)
);

-- every posting we've ever seen
create table if not exists jobs (
  id              bigserial primary key,
  source          text not null,             -- 'greenhouse' | 'adzuna' | 'usajobs' | ...
  source_job_id   text not null,
  company         text not null,
  title           text not null,
  location_raw    text,
  region          text,                      -- 'remote-us' | 'ca-norcal' | 'ca-other' | 'wa' | 'other'
  employment_type text,                      -- 'full_time' | 'contract' | 'c2h' | 'part_time' | 'intern'
  salary_min      int,
  salary_max      int,
  description     text,
  apply_url       text not null,
  posted_at       timestamptz,
  first_seen_at   timestamptz not null default now(),
  last_seen_at    timestamptz not null default now(),
  raw             jsonb,
  unique (source, source_job_id)
);
create index if not exists jobs_first_seen_idx on jobs (first_seen_at desc);
create index if not exists jobs_company_title_idx on jobs (company, title);

-- cross-source dedupe key: the same posting shows up on Greenhouse and an
-- aggregator with different ids. We keep both rows but only one is canonical.
create index if not exists jobs_dedupe_idx
  on jobs (lower(company), lower(title), lower(coalesce(location_raw, '')));

-- hard-filter outcome, written by ingest
create table if not exists job_filters (
  job_id      bigint primary key references jobs(id) on delete cascade,
  passed      bool not null,
  kill_rule   text,                           -- which rule killed it, for tuning
  filtered_at timestamptz not null default now()
);
create index if not exists job_filters_kill_rule_idx on job_filters (kill_rule)
  where kill_rule is not null;

-- LLM score, written by the daily scheduled task
create table if not exists job_scores (
  job_id       bigint primary key references jobs(id) on delete cascade,
  fit          int  not null check (fit between 1 and 10),
  compounding  int  not null check (compounding between 1 and 5),
  title_bucket text,                          -- 'analytics_eng' | 'data_analyst' | ...
  verdict      text not null,                 -- one sentence, shown in the digest
  builds       text,                          -- what the role adds to the resume
  concerns     text,
  soft_flags   text[],
  scored_at    timestamptz not null default now(),
  model        text
);

-- ---------------------------------------------------------------------------
-- Recruiter conflict guard.
--
-- If a staffing agency has submitted Adrien to an employer, applying directly
-- to that employer typically disqualifies him outright, and some employers
-- impose a 6-12 month blackout across the whole company. This table is
-- maintained BY HAND on purpose: a manual insert is the right amount of
-- friction for something this consequential, and there is no reliable way to
-- detect a submission automatically.
--
-- Add a row right after a recruiter call:
--   insert into recruiter_submissions (client_name, agency, role_title, submitted_at)
--   values ('Blue Shield of California', 'TEKsystems', 'Reporting Analyst', current_date);
-- ---------------------------------------------------------------------------
create table if not exists recruiter_submissions (
  id            bigserial primary key,
  client_name   text not null,          -- the EMPLOYER, not the agency
  client_domain text,                   -- optional, improves matching
  agency        text not null,          -- e.g. 'TEKsystems'
  recruiter     text,
  role_title    text,
  submitted_at  date not null,
  expires_at    date,                   -- defaults to submitted_at + 180 days
  active        bool not null default true,
  notes         text
);
create index if not exists recruiter_submissions_client_idx
  on recruiter_submissions (lower(client_name));

create or replace function set_recruiter_submission_expiry()
returns trigger language plpgsql as $$
begin
  if new.expires_at is null then
    new.expires_at := new.submitted_at + interval '180 days';
  end if;
  return new;
end;
$$;

drop trigger if exists recruiter_submissions_expiry on recruiter_submissions;
create trigger recruiter_submissions_expiry
  before insert on recruiter_submissions
  for each row execute function set_recruiter_submission_expiry();

-- the queue and its afterlife
create table if not exists applications (
  id              bigserial primary key,
  job_id          bigint not null unique references jobs(id) on delete cascade,
  status          text not null default 'queued'
                  check (status in ('queued','skipped','applied','screen','interview','offer','rejected','ghosted')),
  skip_reason     text,                       -- e.g. 'recruiter_conflict: TEKsystems 2026-08-14'
  queued_at       timestamptz not null default now(),
  applied_at      timestamptz,
  resume_url      text,                       -- Google Drive link
  cover_url       text,
  last_contact_at timestamptz,
  notes           text
);
create index if not exists applications_status_idx on applications (status);

-- replies detected in Gmail
create table if not exists email_events (
  id              bigserial primary key,
  application_id  bigint references applications(id) on delete cascade,
  gmail_thread_id text not null unique,
  classified_as   text not null check (classified_as in ('rejection','screen','interview','offer','other')),
  subject         text,
  received_at     timestamptz not null
);

-- run telemetry, so failures are visible instead of silent
create table if not exists runs (
  id          bigserial primary key,
  kind        text not null,                  -- 'ingest' | 'score' | 'digest' | 'weekly'
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  ok          bool,
  stats       jsonb,
  error       text
);
create index if not exists runs_kind_started_idx on runs (kind, started_at desc);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

create or replace view v_queue as
  select a.id as application_id, j.*, s.fit, s.compounding, s.verdict,
         s.builds, s.concerns, s.title_bucket
  from applications a
  join jobs j       on j.id = a.job_id
  join job_scores s on s.job_id = j.id
  where a.status = 'queued'
  order by s.fit desc, j.first_seen_at desc;

-- Employers currently owned by an agency. The filter reads this, not the base
-- table, so expiry and deactivation are handled in one place.
create or replace view v_blocked_employers as
  select lower(client_name) as client_key, client_name, client_domain,
         agency, role_title, submitted_at, expires_at
  from recruiter_submissions
  where active = true
    and (expires_at is null or expires_at >= current_date);
