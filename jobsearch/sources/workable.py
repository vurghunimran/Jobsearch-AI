"""Workable job boards.

Public widget API, no credentials:
    GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true

The account is the subdomain in `apply.workable.com/<account>`.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobsearch.matching.normalize import detect_remote_type, normalize_employment_type, strip_html
from jobsearch.models import ATS
from jobsearch.sources.base import JobSource, RawJob, parse_date

WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}"


class WorkableSource(JobSource):
    def __init__(self, account: str) -> None:
        self.account = account.strip().strip("/")
        self.name = f"workable:{self.account}"

    def describe(self) -> str:
        return f"Workable board '{self.account}'"

    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        response = await client.get(
            WIDGET_URL.format(account=self.account), params={"details": "true"}
        )
        response.raise_for_status()
        payload = response.json()
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        company = payload.get("name", "") if isinstance(payload, dict) else ""
        return [self._parse(item, company) for item in jobs]

    def _parse(self, item: dict[str, Any], company: str) -> RawJob:
        loc = item.get("location") or {}
        location = ", ".join(
            str(part) for part in (loc.get("city"), loc.get("region"), loc.get("country")) if part
        )
        telecommuting = bool(loc.get("telecommuting"))

        description = "\n\n".join(
            strip_html(item.get(key))
            for key in ("description", "requirements", "benefits")
            if item.get(key)
        ).strip()

        shortcode = str(item.get("shortcode", ""))
        return RawJob(
            source=self.name,
            external_id=shortcode,
            title=item.get("title", ""),
            company=company or self.account.replace("-", " ").title(),
            url=item.get("url") or item.get("shortlink", ""),
            apply_url=item.get("application_url") or item.get("url", ""),
            description=description,
            location=location,
            remote_type="remote"
            if telecommuting
            else detect_remote_type(location, description[:2000]),
            employment_type=normalize_employment_type(
                item.get("employment_type"), item.get("title")
            ),
            posted_at=parse_date(item.get("published_on") or item.get("created_at")),
            ats=ATS.workable,
            ats_meta={
                "account": self.account,
                "shortcode": shortcode,
                "department": item.get("department", ""),
            },
        )
