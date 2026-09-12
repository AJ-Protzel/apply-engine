"""Normalization tests.

`normalize.py` decides where a posting is and what kind of job it is, and every
rule downstream reads those two fields rather than the source's original text.
It was the least-tested module in the repo and the one with the most subtle
logic in it -- the remote-only board handling in particular, which is a rule
about what an *absent* value means and differs per source.

The last test in this file is not about normalization at all. It asserts that
`profile.yaml` only names employment types the code can actually emit, because
the version before it denied `internship` for a value spelled `intern`: the deny
rule never fired once, the allow-list killed those postings instead, and the
kill log recorded the wrong reason for months.
"""

from __future__ import annotations

import pytest

from apply_engine import normalize
from apply_engine.config import load_profile
from apply_engine.models import EMPLOYMENT_TYPES, REGIONS, Job, RawJob

PROFILE = load_profile()


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Sacramento, CA", "ca-norcal"),
        ("Folsom, California", "ca-norcal"),
        ("San Francisco Bay Area", "ca-norcal"),
        ("San Diego, CA", "ca-other"),
        ("Los Angeles, CA", "ca-other"),
        ("Seattle, WA", "wa"),
        ("Spokane, Washington", "wa"),
        ("Austin, TX", "other"),
        ("Toronto, ON", "other"),
        ("Remote - US", "remote-us"),
        ("Remote (USA)", "remote-us"),
        ("Remote, United States", "remote-us"),
        ("Remote", "remote-us"),
        ("Anywhere", "remote-us"),
        ("Worldwide", "remote-us"),
        ("Remote - Toronto", "other"),
        ("Remote - India", "other"),
        ("Remote - Sacramento, CA", "ca-norcal"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_classify_region(location, expected):
    assert normalize.classify_region(location) == expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (None, "remote-us"),
        ("", "remote-us"),
        ("Remote", "remote-us"),
        ("Worldwide", "remote-us"),
        # A remote-only board naming a place still gets judged on the place: the
        # feeds carry plenty of "Remote - Toronto", and remote-only means
        # "remote from somewhere", not "remote from here".
        ("Remote - Toronto", "other"),
        ("France, Japan, Turkey", "other"),
        ("Remote - Seattle, WA", "wa"),
    ],
)
def test_classify_region_on_remote_only_boards(location, expected):
    assert normalize.classify_region(location, remote_hint=True) == expected


def test_every_remote_hint_can_reach_the_generic_branch():
    """The two remote word lists have to agree.

    A one-word hint that is missing from `_GENERIC_REMOTE` sets `is_remote`,
    fails the generic check, and falls through to be judged as a place -- which
    is how "Worldwide" used to classify as `other`.
    """
    single_words = [h for h in normalize._REMOTE_HINTS if " " not in h]
    assert set(single_words) <= normalize._GENERIC_REMOTE


def test_every_region_is_in_the_declared_vocabulary():
    samples = [None, "", "Remote", "Sacramento, CA", "San Diego, CA", "Seattle, WA",
               "Berlin, Germany", "Remote - India", "Worldwide", "Pune"]
    for sample in samples:
        for hint in (True, False):
            assert normalize.classify_region(sample, remote_hint=hint) in REGIONS


# ---------------------------------------------------------------------------
# Employment type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "title", "expected"),
    [
        ("Full-time", "Data Analyst", "full_time"),
        ("FullTime", "Data Analyst", "full_time"),  # Ashby's spelling, no separator
        ("Permanent", "Data Analyst", "full_time"),
        ("Contract", "Data Analyst", "contract"),
        ("Contract-to-hire", "Data Analyst", "contract_to_hire"),
        ("temp-to-perm", "Data Analyst", "contract_to_hire"),
        ("C2H", "Data Analyst", "contract_to_hire"),
        ("Part-time", "Data Analyst", "part_time"),
        ("internship", "Business Developer", "intern"),
        (None, "Data Engineering Intern", "intern"),
        (None, "Data Analyst", None),
        ("", "", None),
        # Contract-to-hire must win over the plain "contract" it contains.
        ("contract to hire", "", "contract_to_hire"),
    ],
)
def test_classify_employment_type(raw, title, expected):
    assert normalize.classify_employment_type(raw, title) == expected


def test_employment_types_are_in_the_declared_vocabulary():
    for raw in ["Full-time", "Contract", "C2H", "Part-time", "internship", "freelance"]:
        result = normalize.classify_employment_type(raw)
        assert result is None or result in EMPLOYMENT_TYPES


def test_profile_only_names_employment_types_the_code_emits():
    """The drift guard. See the module docstring.

    An allowed-value list in a config file and a second copy in code is a bug
    waiting for its first run, and this one had already fired.
    """
    configured = set(PROFILE["employment_types"]["allow"]) | set(
        PROFILE["employment_types"]["deny"]
    )
    unknown = configured - EMPLOYMENT_TYPES
    assert not unknown, (
        f"profile.yaml names employment types the code never emits: {sorted(unknown)}. "
        "A value not in models.EmploymentType can never match, so the rule silently "
        "never fires."
    )


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------

def test_strip_html_keeps_the_text_and_the_paragraphs():
    html = "<p>Build <b>reports</b>.</p><ul><li>SQL &amp; Python</li></ul>"
    text = normalize.strip_html(html)
    assert "Build reports." in text
    assert "SQL & Python" in text
    assert "<" not in text
    assert "\n\n\n" not in text


@pytest.mark.parametrize("value", [None, "", "   ", "<p></p>"])
def test_strip_html_returns_none_for_nothing(value):
    assert normalize.strip_html(value) is None


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def job(source: str, *, title="Data Analyst", company="Acme Health",
        location="Sacramento, CA", description="Build reports.") -> Job:
    return Job(
        source=source,
        source_job_id=f"{source}-1",
        company=company,
        title=title,
        location_raw=location,
        region=normalize.classify_region(location),
        employment_type="full_time",
        description=description,
        apply_url=f"https://{source}.example.com/1",
    )


def test_dedupe_prefers_the_direct_ats_over_an_aggregator():
    kept = normalize.dedupe([job("remotive"), job("greenhouse")])
    assert len(kept) == 1
    assert kept[0].source == "greenhouse"


def test_dedupe_prefers_the_ats_regardless_of_arrival_order():
    kept = normalize.dedupe([job("greenhouse"), job("remoteok")])
    assert len(kept) == 1
    assert kept[0].source == "greenhouse"


def test_dedupe_between_two_ats_copies_keeps_the_fuller_description():
    thin = job("lever", description="Short.")
    full = job("greenhouse", description="A much longer description of the role.")
    assert normalize.dedupe([thin, full])[0].source == "greenhouse"
    assert normalize.dedupe([full, thin])[0].source == "greenhouse"


def test_dedupe_matches_on_company_title_and_place_case_insensitively():
    a = job("greenhouse", company="ACME HEALTH", title="Data Analyst")
    b = job("remotive", company="acme health", title="data analyst")
    assert len(normalize.dedupe([a, b])) == 1


def test_dedupe_keeps_the_same_title_in_two_cities():
    sacramento = job("greenhouse", location="Sacramento, CA")
    seattle = job("greenhouse", location="Seattle, WA")
    assert len(normalize.dedupe([sacramento, seattle])) == 2


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_normalize_trims_and_classifies():
    raw = RawJob(
        source="greenhouse",
        source_job_id="12345",
        company="  Acme Health  ",
        title="  Data Analyst  ",
        location_raw="  Folsom, CA  ",
        description="<p>Build <b>reports</b>.</p>",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
        employment_type_raw="Full-time",
    )
    job_ = normalize.normalize(raw)

    assert job_.source_job_id == "12345"  # coerced to str for the unique constraint
    assert job_.company == "Acme Health"
    assert job_.title == "Data Analyst"
    assert job_.location_raw == "Folsom, CA"
    assert job_.region == "ca-norcal"
    assert job_.employment_type == "full_time"
    assert job_.description == "Build reports."


def test_blank_location_becomes_none_not_empty_string():
    raw = RawJob(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Data Analyst",
        location_raw="   ",
        apply_url="https://example.com/1",
    )
    assert normalize.normalize(raw).location_raw is None
