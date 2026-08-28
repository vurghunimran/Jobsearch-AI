"""Build the source list from the profile and fetch them all concurrently."""

from __future__ import annotations

import asyncio
import logging

from jobsearch.config import Settings, get_settings
from jobsearch.models import ATS
from jobsearch.profile.schema import Profile
from jobsearch.sources.aggregators import (
    AdzunaSource,
    ArbeitnowSource,
    RemoteOKSource,
    RemotiveSource,
)
from jobsearch.sources.ashby import AshbySource
from jobsearch.sources.base import JobSource, RawJob, SourceCheck, build_http_client
from jobsearch.sources.greenhouse import GreenhouseSource
from jobsearch.sources.lever import LeverSource
from jobsearch.sources.workable import WorkableSource

log = logging.getLogger(__name__)


def build_sources(profile: Profile, settings: Settings | None = None) -> list[JobSource]:
    settings = settings or get_settings()
    config = profile.sources
    sources: list[JobSource] = []

    sources.extend(GreenhouseSource(token) for token in config.greenhouse_boards if token)
    sources.extend(LeverSource(slug) for slug in config.lever_companies if slug)
    sources.extend(AshbySource(org) for org in config.ashby_orgs if org)
    sources.extend(WorkableSource(acct) for acct in config.workable_accounts if acct)

    if config.remotive:
        sources.append(RemotiveSource())
    if config.arbeitnow:
        sources.append(ArbeitnowSource())
    if config.remoteok:
        sources.append(RemoteOKSource())
    if config.adzuna:
        sources.append(
            AdzunaSource(
                app_id=settings.adzuna_app_id,
                app_key=settings.adzuna_app_key,
                country=config.adzuna_country,
                queries=profile.preferences.titles,
                locations=profile.preferences.locations,
                max_days_old=profile.preferences.max_posting_age_days,
            )
        )
    return sources


# Ranking used when the same role turns up on more than one source. A posting
# on a known ATS can be submitted automatically, so it always wins.
_ATS_PRIORITY = {
    ATS.greenhouse: 3,
    ATS.lever: 3,
    ATS.ashby: 3,
    ATS.workable: 2,
    ATS.unknown: 0,
}


def _better(candidate: RawJob, incumbent: RawJob) -> bool:
    cand_rank = _ATS_PRIORITY.get(candidate.ats, 0)
    inc_rank = _ATS_PRIORITY.get(incumbent.ats, 0)
    if cand_rank != inc_rank:
        return cand_rank > inc_rank
    return len(candidate.description) > len(incumbent.description)


def deduplicate(jobs: list[RawJob]) -> list[RawJob]:
    """Collapse the same role found on several sources into one entry."""
    best: dict[str, RawJob] = {}
    for job in jobs:
        if not (job.title and job.company):
            continue
        key = job.fingerprint()
        incumbent = best.get(key)
        if incumbent is None or _better(job, incumbent):
            best[key] = job
    return list(best.values())


async def fetch_all(
    sources: list[JobSource], settings: Settings | None = None
) -> tuple[list[RawJob], list[dict[str, str]]]:
    """Fetch every source concurrently.

    One broken board never fails the run — its error is collected and reported
    in the digest instead.
    """
    settings = settings or get_settings()
    semaphore = asyncio.Semaphore(max(1, settings.source_concurrency))
    errors: list[dict[str, str]] = []

    async with build_http_client(settings) as client:

        async def run(source: JobSource) -> list[RawJob]:
            async with semaphore:
                try:
                    jobs = await source.fetch(client)
                    log.info("%s returned %d postings", source.describe(), len(jobs))
                    return jobs
                except Exception as exc:
                    log.warning("%s failed: %s", source.describe(), exc)
                    errors.append(
                        {"source": source.describe(), "error": f"{type(exc).__name__}: {exc}"}
                    )
                    return []

        results = await asyncio.gather(*(run(source) for source in sources))

    flattened = [job for batch in results for job in batch]
    return deduplicate(flattened), errors


async def check_all(
    sources: list[JobSource], settings: Settings | None = None
) -> list[SourceCheck]:
    """Probe every source. Backs `jobsearch doctor`."""
    settings = settings or get_settings()
    semaphore = asyncio.Semaphore(max(1, settings.source_concurrency))

    async with build_http_client(settings) as client:

        async def run(source: JobSource) -> SourceCheck:
            async with semaphore:
                return await source.check(client)

        return list(await asyncio.gather(*(run(source) for source in sources)))
