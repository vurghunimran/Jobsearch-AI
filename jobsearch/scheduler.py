"""Fires the daily discovery run."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from jobsearch.config import Settings, get_settings

log = logging.getLogger(__name__)


async def _run(settings: Settings) -> None:
    from jobsearch.pipeline import run_discovery

    try:
        stats, queued = await run_discovery(settings)
        log.info("Scheduled run finished: %s, %d queued for review", stats.as_dict(), len(queued))
    except Exception:  # noqa: BLE001 - a failed run must not kill the scheduler
        log.exception("Scheduled discovery run failed")


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    trigger = CronTrigger.from_crontab(settings.digest_cron, timezone=settings.timezone)
    scheduler.add_job(
        _run,
        trigger=trigger,
        args=[settings],
        id="daily-discovery",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    if settings.run_on_startup:
        scheduler.add_job(_run, args=[settings], id="startup-discovery", replace_existing=True)
    scheduler.start()
    log.info("Scheduler started: '%s' (%s)", settings.digest_cron, settings.timezone)
    return scheduler
