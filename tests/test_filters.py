from datetime import UTC, datetime, timedelta

import pytest

from jobsearch.matching.filters import apply_filters
from jobsearch.sources.base import RawJob


def posting(**overrides) -> RawJob:
    base = {
        "source": "test",
        "external_id": "1",
        "title": "Data Analyst",
        "company": "ExampleCorp",
        "url": "https://example.com/1",
        "description": "SQL, Python and dbt.",
        "location": "Tallinn, Estonia",
        "employment_type": "full_time",
    }
    base.update(overrides)
    return RawJob(**base)


class TestFiltersAccept:
    def test_a_matching_posting_passes(self, profile):
        assert apply_filters(posting(), profile).passed

    def test_remote_anywhere_passes(self, profile):
        job = posting(location="Anywhere", remote_type="remote")
        assert apply_filters(job, profile).passed

    def test_unstated_employment_type_is_not_held_against_the_posting(self, profile):
        assert apply_filters(posting(employment_type=""), profile).passed

    def test_undated_posting_is_not_treated_as_stale(self, profile):
        assert apply_filters(posting(posted_at=None), profile).passed


class TestFiltersReject:
    def test_wrong_title(self, profile):
        result = apply_filters(posting(title="Data Engineer"), profile)
        assert not result.passed and "target titles" in result.reason

    def test_excluded_keyword(self, profile):
        result = apply_filters(posting(description="Requires security clearance."), profile)
        assert not result.passed and "security clearance" in result.reason

    def test_out_of_range_seniority(self, profile):
        result = apply_filters(posting(title="Senior Data Analyst"), profile)
        assert not result.passed and "senior" in result.reason

    def test_unwanted_location(self, profile):
        result = apply_filters(posting(location="Tokyo, Japan"), profile)
        assert not result.passed and "Tokyo" in result.reason

    def test_excluded_employment_type(self, profile):
        profile.preferences.employment_types = ["full_time"]
        result = apply_filters(posting(employment_type="internship"), profile)
        assert not result.passed and "internship" in result.reason

    def test_excluded_remote_mode(self, profile):
        profile.preferences.remote_types = ["remote"]
        result = apply_filters(posting(remote_type="onsite"), profile)
        assert not result.passed and "onsite" in result.reason

    def test_excluded_company_matches_loosely(self, profile):
        profile.preferences.exclude_companies = ["example corp"]
        result = apply_filters(posting(), profile)
        assert not result.passed and "exclude list" in result.reason

    def test_stale_posting(self, profile):
        profile.preferences.max_posting_age_days = 7
        old = datetime.now(UTC) - timedelta(days=45)
        result = apply_filters(posting(posted_at=old), profile)
        assert not result.passed and "45 days ago" in result.reason

    def test_required_keyword_missing(self, profile):
        profile.preferences.keywords = ["dbt", "looker"]
        result = apply_filters(posting(description="Only Excel here."), profile)
        assert not result.passed and "required keywords" in result.reason


class TestSponsorship:
    @pytest.fixture
    def needs_sponsorship(self, profile):
        profile.work_authorization.requires_sponsorship = True
        profile.preferences.must_not_require_sponsorship_free = True
        return profile

    def test_drops_postings_that_refuse_to_sponsor(self, needs_sponsorship):
        job = posting(description="You must be authorized to work in the US without sponsorship.")
        result = apply_filters(job, needs_sponsorship)
        assert not result.passed and "sponsor" in result.reason

    def test_keeps_silent_postings(self, needs_sponsorship):
        assert apply_filters(posting(), needs_sponsorship).passed

    def test_ignored_when_the_candidate_needs_no_sponsorship(self, profile):
        profile.preferences.must_not_require_sponsorship_free = True
        profile.work_authorization.requires_sponsorship = False
        job = posting(description="Must be authorized to work without sponsorship.")
        assert apply_filters(job, profile).passed
