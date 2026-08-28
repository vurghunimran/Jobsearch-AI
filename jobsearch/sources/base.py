"""The contract every job source implements."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from jobsearch.matching.normalize import slugify
from jobsearch.models import ATS, Job

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RawJob:
    """A posting as it came off a board, before any filtering."""

    source: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    location: str = ""
    remote_type: str = ""
    employment_type: str = ""
    salary_text: str = ""
    posted_at: datetime | None = None
    apply_url: str = ""
    ats: ATS = ATS.unknown
    ats_meta: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Identity of the *role*, not of the listing.

        Deliberately excludes the source, so the same job found on a company
        board and on an aggregator collapses into one row.
        """
        key = "|".join(
            (
                slugify(self.company),
                slugify(self.title),
                slugify(self.location)[:40],
            )
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def to_job(self) -> Job:
        return Job(
            fingerprint=self.fingerprint(),
            source=self.source,
            ats=self.ats,
            external_id=str(self.external_id),
            url=self.url,
            apply_url=self.apply_url or self.url,
            company=self.company.strip(),
            title=self.title.strip(),
            location=self.location.strip(),
            remote_type=self.remote_type,
            employment_type=self.employment_type,
            salary_text=self.salary_text,
            description=self.description,
            posted_at=self.posted_at,
            ats_meta=self.ats_meta,
        )


@dataclass(slots=True)
class SourceCheck:
    """Result of `jobsearch doctor` probing one source."""

    name: str
    ok: bool
    count: int = 0
    detail: str = ""


class JobSource(ABC):
    """Fetches postings from one board or aggregator."""

    #: Stable identifier stored on each Job row.
    name: str = "source"

    #: Human-readable description shown by `jobsearch doctor`.
    def describe(self) -> str:
        return self.name

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[RawJob]:
        """Return every posting this source currently offers.

        Filtering happens later, centrally — a source should not try to be
        clever about relevance. It may pass search terms to the upstream API
        when the API requires them (Adzuna), but must not drop rows itself.
        """

    async def check(self, client: httpx.AsyncClient) -> SourceCheck:
        try:
            jobs = await self.fetch(client)
        except httpx.HTTPStatusError as exc:
            return SourceCheck(self.describe(), False, 0, f"HTTP {exc.response.status_code}")
        except Exception as exc:
            return SourceCheck(self.describe(), False, 0, f"{type(exc).__name__}: {exc}")
        detail = "" if jobs else "reachable but returned 0 postings"
        return SourceCheck(self.describe(), True, len(jobs), detail)


def parse_date(value: Any) -> datetime | None:
    """Parse the many date shapes boards emit; never raise."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Boards emit both seconds and milliseconds since epoch.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from dateutil import parser as date_parser

        parsed = date_parser.parse(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:
        return None


def build_http_client(settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept": "application/json, text/plain, */*",
        },
    )
