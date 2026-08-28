"""Greenhouse job boards.

Public board API, no credentials:
    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

The board token is the last path segment of a company's board URL, e.g.
`job-boards.greenhouse.io/stripe` -> "stripe".
"""

from __future__ import annotations

import html as html_lib
from typing import Any

import httpx

from jobsearch.matching.normalize import detect_remote_type, normalize_employment_type, strip_html
from jobsearch.models import ATS
from jobsearch.sources.base import JobSource, RawJob, parse_date

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"


class GreenhouseSource(JobSource):
    def __init__(self, board_token: str) -> None:
        self.board_token = board_token.strip().strip("/")
        self.name = f"greenhouse:{self.board_token}"

    def describe(self) -> str:
        return f"Greenhouse board '{self.board_token}'"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(
            BOARD_URL.format(token=self.board_token), params={"content": "true"}
        )
        response.raise_for_status()
        payload = response.json()
        return [self._parse(item) for item in payload.get("jobs", [])]

    def _parse(self, item: dict[str, Any]) -> RawJob:
        # Greenhouse HTML-escapes the already-HTML description, so unescape once
        # to get real markup back before stripping tags.
        description = strip_html(html_lib.unescape(item.get("content") or ""))
        location = (item.get("location") or {}).get("name", "") or ""
        metadata = {
            str(m.get("name")): m.get("value")
            for m in (item.get("metadata") or [])
            if isinstance(m, dict)
        }
        job_id = str(item.get("id", ""))
        return RawJob(
            source=self.name,
            external_id=job_id,
            title=item.get("title", ""),
            company=self._company_name(item),
            url=item.get("absolute_url", ""),
            apply_url=item.get("absolute_url", ""),
            description=description,
            location=location,
            remote_type=detect_remote_type(location, item.get("title"), description[:2000]),
            employment_type=normalize_employment_type(
                item.get("title"), str(metadata.get("Employment Type", "")), description[:2000]
            ),
            posted_at=parse_date(item.get("first_published") or item.get("updated_at")),
            ats=ATS.greenhouse,
            ats_meta={
                "board_token": self.board_token,
                "job_id": job_id,
                "questions_url": JOB_URL.format(token=self.board_token, job_id=job_id),
                "metadata": metadata,
            },
        )

    def _company_name(self, item: dict[str, Any]) -> str:
        company = (item.get("company_name") or "").strip()
        return company or self.board_token.replace("-", " ").title()


async def fetch_questions(client: httpx.AsyncClient, board_token: str, job_id: str) -> list[dict]:
    """Fetch a posting's application form questions.

    Called only for jobs that reach your review queue, so the bulk discovery
    pass stays to one request per board.
    """
    response = await client.get(
        JOB_URL.format(token=board_token, job_id=job_id), params={"questions": "true"}
    )
    response.raise_for_status()
    return response.json().get("questions", []) or []
