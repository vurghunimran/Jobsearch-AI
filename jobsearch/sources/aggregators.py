"""Cross-company job aggregators.

These give breadth that individual company boards cannot. Their postings
usually link out to an employer's own ATS, so the apply route is resolved
later from the destination URL rather than from the aggregator itself.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobsearch.matching.normalize import detect_remote_type, normalize_employment_type, strip_html
from jobsearch.sources.base import JobSource, RawJob, parse_date

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
REMOTEOK_URL = "https://remoteok.com/api"
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


class RemotiveSource(JobSource):
    """Remote-only board. Full descriptions, generous free API."""

    name = "remotive"

    def __init__(self, limit: int = 200) -> None:
        self.limit = limit

    def describe(self) -> str:
        return "Remotive (remote jobs)"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(REMOTIVE_URL, params={"limit": self.limit})
        response.raise_for_status()
        payload = response.json()
        return [self._parse(item) for item in payload.get("jobs", [])]

    def _parse(self, item: dict[str, Any]) -> RawJob:
        description = strip_html(item.get("description"))
        location = item.get("candidate_required_location") or "Remote"
        return RawJob(
            source=self.name,
            external_id=str(item.get("id", "")),
            title=item.get("title", ""),
            company=item.get("company_name", ""),
            url=item.get("url", ""),
            description=description,
            location=location,
            remote_type="remote",
            employment_type=normalize_employment_type(item.get("job_type"), item.get("title")),
            salary_text=item.get("salary") or "",
            posted_at=parse_date(item.get("publication_date")),
        )


class ArbeitnowSource(JobSource):
    """Europe-heavy board (strong German-market coverage). Paginated."""

    name = "arbeitnow"

    def __init__(self, max_pages: int = 3) -> None:
        self.max_pages = max_pages

    def describe(self) -> str:
        return "Arbeitnow (EU jobs)"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, self.max_pages + 1):
            response = await client.get(ARBEITNOW_URL, params={"page": page})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", [])
            if not rows:
                break
            jobs.extend(self._parse(item) for item in rows)
        return jobs

    def _parse(self, item: dict[str, Any]) -> RawJob:
        description = strip_html(item.get("description"))
        tags = item.get("tags") or []
        job_types = item.get("job_types") or []
        return RawJob(
            source=self.name,
            external_id=str(item.get("slug", "")),
            title=item.get("title", ""),
            company=item.get("company_name", ""),
            url=item.get("url", ""),
            description=description,
            location=item.get("location", ""),
            remote_type="remote" if item.get("remote") else detect_remote_type(description[:2000]),
            employment_type=normalize_employment_type(
                " ".join(str(t) for t in job_types),
                " ".join(str(t) for t in tags),
                item.get("title"),
            ),
            posted_at=parse_date(item.get("created_at")),
        )


class RemoteOKSource(JobSource):
    """RemoteOK. Requires attribution when displaying results publicly."""

    name = "remoteok"

    def describe(self) -> str:
        return "RemoteOK"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(REMOTEOK_URL)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        # The first element is a legal/attribution notice, not a posting.
        rows = [row for row in payload if isinstance(row, dict) and row.get("position")]
        return [self._parse(item) for item in rows]

    def _parse(self, item: dict[str, Any]) -> RawJob:
        description = strip_html(item.get("description"))
        salary_min, salary_max = item.get("salary_min"), item.get("salary_max")
        salary = f"{salary_min}–{salary_max} USD" if salary_min and salary_max else ""
        return RawJob(
            source=self.name,
            external_id=str(item.get("id") or item.get("slug", "")),
            title=item.get("position", ""),
            company=item.get("company", ""),
            url=item.get("url", ""),
            apply_url=item.get("apply_url") or item.get("url", ""),
            description=description,
            location=item.get("location") or "Remote",
            remote_type="remote",
            employment_type=normalize_employment_type(
                " ".join(str(t) for t in item.get("tags") or []), item.get("position")
            ),
            salary_text=salary,
            posted_at=parse_date(item.get("epoch") or item.get("date")),
        )


class AdzunaSource(JobSource):
    """Adzuna aggregate search. Needs free API credentials.

    Unlike the other sources this one *must* be given search terms — Adzuna has
    no "return everything" mode — so the user's target titles are passed
    through as queries, one request per title.

    Note: Adzuna returns truncated description snippets. Scoring still works,
    but generated documents lean more on the title and company than usual.
    """

    name = "adzuna"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        country: str,
        queries: list[str],
        locations: list[str],
        max_days_old: int = 30,
        results_per_page: int = 50,
    ) -> None:
        self.app_id = app_id
        self.app_key = app_key
        self.country = (country or "gb").lower()
        self.queries = queries or [""]
        self.locations = locations or [""]
        self.max_days_old = max_days_old
        self.results_per_page = results_per_page

    def describe(self) -> str:
        return f"Adzuna ({self.country.upper()})"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        if not (self.app_id and self.app_key):
            raise RuntimeError("Adzuna is enabled but ADZUNA_APP_ID / ADZUNA_APP_KEY are unset.")
        jobs: list[RawJob] = []
        seen: set[str] = set()
        # One query per (title, location) pair, capped so a broad profile does
        # not fan out into hundreds of requests.
        pairs = [(q, loc) for q in self.queries[:6] for loc in self.locations[:3]] or [("", "")]
        for query, where in pairs[:12]:
            params: dict[str, Any] = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": self.results_per_page,
                "max_days_old": self.max_days_old,
                "content-type": "application/json",
            }
            if query:
                params["what"] = query
            if where:
                params["where"] = where
            response = await client.get(
                ADZUNA_URL.format(country=self.country, page=1), params=params
            )
            response.raise_for_status()
            for item in response.json().get("results", []):
                job = self._parse(item)
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                jobs.append(job)
        return jobs

    def _parse(self, item: dict[str, Any]) -> RawJob:
        location = (item.get("location") or {}).get("display_name", "")
        description = strip_html(item.get("description"))
        salary_min, salary_max = item.get("salary_min"), item.get("salary_max")
        salary = f"{salary_min:.0f}–{salary_max:.0f}" if salary_min and salary_max else ""
        return RawJob(
            source=self.name,
            external_id=str(item.get("id", "")),
            title=item.get("title", ""),
            company=(item.get("company") or {}).get("display_name", ""),
            url=item.get("redirect_url", ""),
            description=description,
            location=location,
            remote_type=detect_remote_type(location, item.get("title"), description),
            employment_type=normalize_employment_type(
                item.get("contract_time"), item.get("contract_type"), item.get("title")
            ),
            salary_text=salary,
            posted_at=parse_date(item.get("created")),
        )
