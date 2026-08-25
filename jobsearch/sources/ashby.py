"""Ashby job boards.

Public posting API, no credentials:
    GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true

The org name is the path segment in `jobs.ashbyhq.com/<org>`.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobsearch.matching.normalize import detect_remote_type, normalize_employment_type, strip_html
from jobsearch.models import ATS
from jobsearch.sources.base import JobSource, RawJob, parse_date

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{org}"

_EMPLOYMENT_MAP = {
    "fulltime": "full_time",
    "parttime": "part_time",
    "intern": "internship",
    "contract": "contract",
    "temporary": "temporary",
}


class AshbySource(JobSource):
    def __init__(self, org: str) -> None:
        self.org = org.strip().strip("/")
        self.name = f"ashby:{self.org}"

    def describe(self) -> str:
        return f"Ashby board '{self.org}'"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(
            BOARD_URL.format(org=self.org), params={"includeCompensation": "true"}
        )
        response.raise_for_status()
        payload = response.json()
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        return [self._parse(item) for item in jobs if item.get("isListed", True)]

    def _parse(self, item: dict[str, Any]) -> RawJob:
        description = strip_html(item.get("descriptionHtml")) or (
            item.get("descriptionPlain") or ""
        )
        location = item.get("location") or ""
        secondary = item.get("secondaryLocations") or []
        if secondary:
            extra = ", ".join(
                loc.get("location", "") for loc in secondary if isinstance(loc, dict)
            ).strip(", ")
            if extra:
                location = f"{location}; {extra}" if location else extra

        raw_type = (item.get("employmentType") or "").lower()
        employment_type = _EMPLOYMENT_MAP.get(raw_type) or normalize_employment_type(
            raw_type, item.get("title")
        )
        remote_type = (
            "remote"
            if item.get("isRemote")
            else detect_remote_type(location, item.get("title"), description[:2000])
        )

        job_id = str(item.get("id", ""))
        return RawJob(
            source=self.name,
            external_id=job_id,
            title=item.get("title", ""),
            company=item.get("organizationName") or self.org.replace("-", " ").title(),
            url=item.get("jobUrl", ""),
            apply_url=item.get("applyUrl") or item.get("jobUrl", ""),
            description=description,
            location=location,
            remote_type=remote_type,
            employment_type=employment_type,
            salary_text=self._compensation(item),
            posted_at=parse_date(item.get("publishedAt") or item.get("updatedAt")),
            ats=ATS.ashby,
            ats_meta={
                "org": self.org,
                "job_posting_id": job_id,
                "department": item.get("department", ""),
                "team": item.get("team", ""),
            },
        )

    def _compensation(self, item: dict[str, Any]) -> str:
        comp = item.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or comp.get("summaryComponents")
        return summary if isinstance(summary, str) else ""
