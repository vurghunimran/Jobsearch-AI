"""Hard preference filters.

These run before any model call. Their job is to be cheap, predictable and
explainable: every rejected posting keeps a human-readable reason, so when the
queue looks wrong you can see exactly which rule did it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jobsearch.matching.normalize import (
    contains_any,
    detect_seniority,
    location_matches,
    slugify,
    title_matches,
)
from jobsearch.profile.schema import Preferences, Profile

# Phrases employers use to say they will not sponsor a visa.
NO_SPONSORSHIP_PHRASES = [
    "without sponsorship",
    "no sponsorship",
    "not provide sponsorship",
    "not offer sponsorship",
    "unable to sponsor",
    "cannot sponsor",
    "do not sponsor",
    "does not sponsor",
    "no visa sponsorship",
    "sponsorship is not available",
    "must be authorized to work",
    "must already have the right to work",
]


class Posting(Protocol):
    """The fields a filter needs. Both RawJob and the Job table satisfy this."""

    title: str
    company: str
    location: str
    remote_type: str
    employment_type: str
    description: str
    posted_at: datetime | None


@dataclass(slots=True)
class FilterResult:
    passed: bool
    reason: str = ""


PASS = FilterResult(True)


def apply_filters(job: Posting, profile: Profile) -> FilterResult:
    """Return PASS, or a failure carrying the rule that rejected the posting."""
    prefs: Preferences = profile.preferences
    title = job.title or ""
    haystack = f"{title}\n{job.description or ''}"

    excluded_company = _matches_company(job.company, prefs.exclude_companies)
    if excluded_company:
        return FilterResult(False, f"company on your exclude list ({excluded_company})")

    banned_title = contains_any(title, prefs.exclude_titles)
    if banned_title:
        return FilterResult(False, f"title contains excluded term '{banned_title}'")

    if not title_matches(title, prefs.titles):
        return FilterResult(False, "title does not match any of your target titles")

    banned_keyword = contains_any(haystack, prefs.exclude_keywords)
    if banned_keyword:
        return FilterResult(False, f"posting contains excluded keyword '{banned_keyword}'")

    if prefs.keywords and not contains_any(haystack, prefs.keywords):
        return FilterResult(False, "posting mentions none of your required keywords")

    if job.remote_type and prefs.remote_types and job.remote_type not in prefs.remote_types:
        return FilterResult(False, f"{job.remote_type} work, which you excluded")

    if not location_matches(
        job.location, job.remote_type, prefs.locations, prefs.accept_worldwide_remote
    ):
        return FilterResult(
            False, f"location '{job.location or 'unstated'}' is outside your targets"
        )

    if (
        job.employment_type
        and prefs.employment_types
        and job.employment_type not in prefs.employment_types
    ):
        return FilterResult(
            False, f"{job.employment_type.replace('_', '-')} role, which you excluded"
        )

    if prefs.seniority:
        level = detect_seniority(title)
        if level and level not in prefs.seniority:
            return FilterResult(False, f"{level}-level role, outside your seniority range")

    age_result = _check_age(job.posted_at, prefs.max_posting_age_days)
    if not age_result.passed:
        return age_result

    if prefs.must_not_require_sponsorship_free and profile.work_authorization.requires_sponsorship:
        phrase = contains_any(job.description or "", NO_SPONSORSHIP_PHRASES)
        if phrase:
            return FilterResult(False, f"employer states they do not sponsor ('{phrase}')")

    return PASS


def _matches_company(company: str, excluded: list[str]) -> str | None:
    """Match company names ignoring case, accents, spacing and punctuation.

    People write "Example Corp" in their exclude list and expect it to catch
    "ExampleCorp" and "example-corp" too, so separators are dropped entirely
    before comparing.
    """
    if not excluded:
        return None
    needle = slugify(company).replace("-", "")
    for entry in excluded:
        entry_slug = slugify(entry).replace("-", "")
        if entry_slug and entry_slug in needle:
            return entry
    return None


def _check_age(posted_at: datetime | None, max_days: int) -> FilterResult:
    if posted_at is None or max_days <= 0:
        # An unstated date is not evidence of staleness; let scoring decide.
        return PASS
    reference = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    cutoff = datetime.now(UTC) - timedelta(days=max_days)
    if reference < cutoff:
        age_days = (datetime.now(UTC) - reference).days
        return FilterResult(False, f"posted {age_days} days ago (limit {max_days})")
    return PASS
