"""Connector tests.

Each source is exercised through its real `fetch` path against a mocked HTTP
response shaped like the upstream API, so a change to URL, params or parsing is
caught here rather than in production.
"""

import httpx
import pytest
import respx

from jobsearch.models import ATS
from jobsearch.sources.aggregators import ArbeitnowSource, RemoteOKSource, RemotiveSource
from jobsearch.sources.ashby import AshbySource
from jobsearch.sources.base import RawJob, parse_date
from jobsearch.sources.greenhouse import GreenhouseSource
from jobsearch.sources.lever import LeverSource
from jobsearch.sources.registry import deduplicate
from jobsearch.sources.workable import WorkableSource


@pytest.fixture
def client():
    return httpx.AsyncClient(follow_redirects=True)


@respx.mock
async def test_greenhouse(client):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 4567,
                        "title": "Data Analyst, Growth",
                        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/4567",
                        "location": {"name": "Tallinn, Estonia"},
                        "updated_at": "2026-08-01T10:00:00-04:00",
                        # Greenhouse HTML-escapes already-HTML content.
                        "content": (
                            "&lt;p&gt;Own growth reporting.&lt;/p&gt;"
                            "&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;"
                        ),
                        "metadata": [{"name": "Employment Type", "value": "Full-time"}],
                    }
                ]
            },
        )
    )
    jobs = await GreenhouseSource("examplecorp").fetch(client)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Data Analyst, Growth"
    assert job.location == "Tallinn, Estonia"
    assert job.ats is ATS.greenhouse
    assert job.employment_type == "full_time"
    # The double-escaped description must come back as readable text.
    assert "Own growth reporting." in job.description
    assert "- SQL" in job.description
    assert "&lt;" not in job.description
    assert job.ats_meta["board_token"] == "examplecorp"
    assert job.ats_meta["job_id"] == "4567"


@respx.mock
async def test_lever(client):
    respx.get("https://api.lever.co/v0/postings/examplecorp").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc-123",
                    "text": "Analytics Engineer",
                    "categories": {
                        "location": "Berlin",
                        "commitment": "Full-time",
                        "team": "Data",
                    },
                    "workplaceType": "hybrid",
                    "descriptionPlain": "Build the warehouse.",
                    "lists": [{"text": "Requirements", "content": "<ul><li>dbt</li></ul>"}],
                    "additionalPlain": "We offer relocation.",
                    "hostedUrl": "https://jobs.lever.co/examplecorp/abc-123",
                    "applyUrl": "https://jobs.lever.co/examplecorp/abc-123/apply",
                    "createdAt": 1754006400000,
                }
            ],
        )
    )
    jobs = await LeverSource("examplecorp").fetch(client)
    job = jobs[0]
    assert job.ats is ATS.lever
    assert job.remote_type == "hybrid"
    assert job.employment_type == "full_time"
    # All three description sections are stitched together.
    assert "Build the warehouse." in job.description
    assert "Requirements" in job.description and "- dbt" in job.description
    assert "We offer relocation." in job.description
    assert job.apply_url.endswith("/apply")
    assert job.ats_meta["posting_id"] == "abc-123"
    assert job.posted_at is not None and job.posted_at.year == 2025


@respx.mock
async def test_ashby(client):
    respx.get("https://api.ashbyhq.com/posting-api/job-board/examplecorp").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "job-1",
                        "title": "Data Analyst",
                        "location": "Remote",
                        "secondaryLocations": [{"location": "Lisbon"}],
                        "employmentType": "FullTime",
                        "isRemote": True,
                        "isListed": True,
                        "descriptionHtml": "<p>Analytics work.</p>",
                        "publishedAt": "2026-08-10T09:00:00Z",
                        "jobUrl": "https://jobs.ashbyhq.com/examplecorp/job-1",
                        "organizationName": "ExampleCorp",
                    },
                    {"id": "job-2", "title": "Hidden", "isListed": False},
                ]
            },
        )
    )
    jobs = await AshbySource("examplecorp").fetch(client)
    assert len(jobs) == 1, "unlisted postings must be skipped"
    job = jobs[0]
    assert job.ats is ATS.ashby
    assert job.remote_type == "remote"
    assert job.employment_type == "full_time"
    assert job.company == "ExampleCorp"
    assert "Lisbon" in job.location


@respx.mock
async def test_workable(client):
    respx.get("https://apply.workable.com/api/v1/widget/accounts/examplecorp").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "ExampleCorp",
                "jobs": [
                    {
                        "title": "Data Analyst",
                        "shortcode": "ABC123",
                        "url": "https://apply.workable.com/examplecorp/j/ABC123",
                        "application_url": "https://apply.workable.com/examplecorp/j/ABC123/apply",
                        "location": {
                            "city": "Tallinn",
                            "region": "Harju",
                            "country": "Estonia",
                            "telecommuting": True,
                        },
                        "employment_type": "Full-time",
                        "description": "<p>Reporting.</p>",
                        "requirements": "<p>SQL.</p>",
                        "published_on": "2026-08-12",
                    }
                ],
            },
        )
    )
    job = (await WorkableSource("examplecorp").fetch(client))[0]
    assert job.ats is ATS.workable
    assert job.company == "ExampleCorp"
    assert job.remote_type == "remote"
    assert "Tallinn" in job.location and "Estonia" in job.location
    assert "Reporting." in job.description and "SQL." in job.description


@respx.mock
async def test_remotive(client):
    respx.get("https://remotive.com/api/remote-jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 99,
                        "title": "Data Analyst",
                        "company_name": "RemoteCo",
                        "url": "https://remotive.com/remote-jobs/99",
                        "candidate_required_location": "Europe",
                        "job_type": "full_time",
                        "salary": "$60k - $80k",
                        "publication_date": "2026-08-15T00:00:00",
                        "description": "<p>Dashboards.</p>",
                    }
                ]
            },
        )
    )
    job = (await RemotiveSource().fetch(client))[0]
    assert job.remote_type == "remote"
    assert job.salary_text == "$60k - $80k"
    assert job.description == "Dashboards."


@respx.mock
async def test_arbeitnow_paginates_and_stops_when_empty(client):
    route = respx.get("https://www.arbeitnow.com/api/job-board-api")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "slug": "a-1",
                        "title": "Datenanalyst",
                        "company_name": "BerlinCo",
                        "url": "https://arbeitnow.com/view/a-1",
                        "description": "<p>SQL.</p>",
                        "remote": True,
                        "location": "Berlin",
                        "job_types": ["Vollzeit"],
                        "tags": [],
                        "created_at": 1755000000,
                    }
                ]
            },
        ),
        httpx.Response(200, json={"data": []}),
    ]
    jobs = await ArbeitnowSource(max_pages=3).fetch(client)
    assert len(jobs) == 1
    assert jobs[0].employment_type == "full_time"
    assert jobs[0].remote_type == "remote"
    assert route.call_count == 2, "must stop paging on the first empty page"


@respx.mock
async def test_remoteok_skips_the_legal_notice(client):
    respx.get("https://remoteok.com/api").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"legal": "See remoteok.com/api for terms"},
                {
                    "id": "77",
                    "position": "Data Analyst",
                    "company": "RemoteOKCo",
                    "url": "https://remoteok.com/l/77",
                    "description": "<p>Work.</p>",
                    "tags": ["full time"],
                    "salary_min": 50000,
                    "salary_max": 70000,
                    "epoch": 1755000000,
                },
            ],
        )
    )
    jobs = await RemoteOKSource().fetch(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"
    assert "50000" in jobs[0].salary_text


class TestParseDate:
    def test_handles_seconds_milliseconds_iso_and_junk(self):
        assert parse_date(1755000000).year == 2025
        assert parse_date(1755000000000).year == 2025
        assert parse_date("2026-08-15T00:00:00Z").year == 2026
        assert parse_date("not a date") is None
        assert parse_date(None) is None
        assert parse_date("") is None

    def test_naive_datetimes_become_utc(self):
        assert parse_date("2026-08-15 09:00:00").tzinfo is not None


class TestDeduplication:
    def _job(self, source, ats=ATS.unknown, description="short"):
        return RawJob(
            source=source,
            external_id="x",
            title="Data Analyst",
            company="ExampleCorp",
            url="https://example.com",
            location="Tallinn",
            description=description,
            ats=ats,
        )

    def test_same_role_from_two_sources_collapses(self):
        jobs = deduplicate([self._job("remotive"), self._job("greenhouse:x", ATS.greenhouse)])
        assert len(jobs) == 1

    def test_the_directly_applicable_copy_wins(self):
        jobs = deduplicate(
            [
                self._job("remotive", description="a much longer description here"),
                self._job("greenhouse:x", ATS.greenhouse, description="short"),
            ]
        )
        # A Greenhouse posting can be submitted automatically, so it beats a
        # longer aggregator copy that cannot.
        assert jobs[0].ats is ATS.greenhouse

    def test_richer_description_wins_within_the_same_tier(self):
        jobs = deduplicate(
            [
                self._job("remotive", description="short"),
                self._job("arbeitnow", description="much longer text"),
            ]
        )
        assert jobs[0].description == "much longer text"

    def test_drops_rows_missing_a_title_or_company(self):
        broken = RawJob(source="s", external_id="1", title="", company="X", url="u")
        assert deduplicate([broken]) == []
