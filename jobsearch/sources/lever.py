"""Lever job boards.

Public postings API, no credentials:
    GET https://api.lever.co/v0/postings/{company}?mode=json

The company slug is the path segment in `jobs.lever.co/<slug>`.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobsearch.matching.normalize import detect_remote_type, normalize_employment_type, strip_html
from jobsearch.models import ATS
from jobsearch.sources.base import JobSource, RawJob, parse_date

POSTINGS_URL = "https://api.lever.co/v0/postings/{company}"


class LeverSource(JobSource):
    def __init__(self, company: str) -> None:
        self.company = company.strip().strip("/")
        self.name = f"lever:{self.company}"

    def describe(self) -> str:
        return f"Lever board '{self.company}'"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(
            POSTINGS_URL.format(company=self.company), params={"mode": "json"}
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [self._parse(item) for item in payload]

    def _parse(self, item: dict[str, Any]) -> RawJob:
        categories = item.get("categories") or {}
        location = categories.get("location") or ""
        commitment = categories.get("commitment") or ""
        workplace = item.get("workplaceType") or ""

        # Lever splits the description across `description`, the `lists`
        # sections (Responsibilities, Requirements, ...) and `additional`.
        parts = [item.get("descriptionPlain") or strip_html(item.get("description"))]
        for block in item.get("lists") or []:
            heading = (block.get("text") or "").strip()
            body = strip_html(block.get("content"))
            if heading or body:
                parts.append(f"{heading}\n{body}".strip())
        additional = item.get("additionalPlain") or strip_html(item.get("additional"))
        if additional:
            parts.append(additional)
        description = "\n\n".join(p for p in parts if p).strip()

        posting_id = str(item.get("id", ""))
        return RawJob(
            source=self.name,
            external_id=posting_id,
            title=item.get("text", ""),
            company=self.company.replace("-", " ").title(),
            url=item.get("hostedUrl", ""),
            apply_url=item.get("applyUrl") or item.get("hostedUrl", ""),
            description=description,
            location=location,
            remote_type=detect_remote_type(
                workplace, location, item.get("text"), description[:2000]
            ),
            employment_type=normalize_employment_type(commitment, item.get("text")),
            posted_at=parse_date(item.get("createdAt") or item.get("publishedAt")),
            ats=ATS.lever,
            ats_meta={
                "company": self.company,
                "posting_id": posting_id,
                "team": categories.get("team", ""),
                "department": categories.get("department", ""),
                "commitment": commitment,
            },
        )
