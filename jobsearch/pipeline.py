"""The daily run: discover -> filter -> score -> write -> queue -> notify."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, func, select

from jobsearch.apply.questions import fetch_questions
from jobsearch.config import Settings, get_settings
from jobsearch.db import session_scope
from jobsearch.documents.generator import (
    answer_screening_questions,
    choose_document_kinds,
    generate_documents,
)
from jobsearch.matching.filters import apply_filters
from jobsearch.matching.scorer import JobFit, score_jobs
from jobsearch.models import (
    Application,
    ApplicationStatus,
    Document,
    Job,
    JobStatus,
    RunLog,
)
from jobsearch.profile.loader import cv_text, load_profile
from jobsearch.profile.schema import Profile
from jobsearch.sources.base import build_http_client
from jobsearch.sources.registry import build_sources, fetch_all

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    fetched: int = 0
    new: int = 0
    filtered_out: int = 0
    scored: int = 0
    matched: int = 0
    queued: int = 0
    documents: int = 0
    score_errors: int = 0
    source_errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "new": self.new,
            "filtered_out": self.filtered_out,
            "scored": self.scored,
            "matched": self.matched,
            "queued": self.queued,
            "documents": self.documents,
            "score_errors": self.score_errors,
        }


async def run_discovery(
    settings: Settings | None = None, notify: bool = True
) -> tuple[RunStats, list[Application]]:
    """One full pass. Returns the stats and the applications now awaiting review."""
    settings = settings or get_settings()
    profile = load_profile(settings)
    resume_text = cv_text(profile, settings)
    stats = RunStats()

    run = RunLog()
    with session_scope(settings) as session:
        session.add(run)
        session.commit()
        run_id = run.id

    try:
        raw_jobs, source_errors = await fetch_all(build_sources(profile, settings), settings)
        stats.fetched = len(raw_jobs)
        stats.source_errors = source_errors

        with session_scope(settings) as session:
            new_jobs = _persist(session, raw_jobs)
            stats.new = len(new_jobs)
            to_score = _filter(session, new_jobs, profile, stats)
            to_score = to_score[: settings.max_jobs_scored_per_run]

        if to_score:
            with session_scope(settings) as session:
                jobs = _reload(session, [j.id for j in to_score])
                results = score_jobs(jobs, profile, resume_text, settings)
                matched_ids = _apply_scores(session, jobs, results, settings, stats)
        else:
            matched_ids = []

        queued = await _build_applications(matched_ids, profile, resume_text, settings, stats)

        _finish(run_id, settings, stats, ok=True)
    except Exception as exc:
        log.exception("Discovery run failed")
        _finish(run_id, settings, stats, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise

    if notify and queued:
        _send_digest(queued, stats, settings)
    return stats, queued


def _persist(session: Session, raw_jobs: list) -> list[Job]:
    """Insert postings we have never seen. Returns only the newly created rows."""
    if not raw_jobs:
        return []
    incoming = {job.fingerprint(): job for job in raw_jobs}
    existing = set(
        session.exec(select(Job.fingerprint).where(Job.fingerprint.in_(list(incoming)))).all()
    )
    created: list[Job] = []
    for fingerprint, raw in incoming.items():
        if fingerprint in existing:
            continue
        job = raw.to_job()
        session.add(job)
        created.append(job)
    session.commit()
    return created


def _filter(session: Session, jobs: list[Job], profile: Profile, stats: RunStats) -> list[Job]:
    survivors: list[Job] = []
    for job in jobs:
        result = apply_filters(job, profile)
        if result.passed:
            survivors.append(job)
        else:
            job.status = JobStatus.filtered_out
            job.filter_reason = result.reason
            session.add(job)
            stats.filtered_out += 1
    session.commit()
    return survivors


def _reload(session: Session, job_ids: list[int]) -> list[Job]:
    if not job_ids:
        return []
    return list(session.exec(select(Job).where(Job.id.in_(job_ids))).all())


def _apply_scores(
    session: Session,
    jobs: list[Job],
    results: dict[int, JobFit | Exception],
    settings: Settings,
    stats: RunStats,
) -> list[int]:
    matched: list[tuple[int, int]] = []
    for job in jobs:
        outcome = results.get(job.id)
        if outcome is None or isinstance(outcome, Exception):
            stats.score_errors += 1
            continue

        job.score = outcome.score
        job.verdict = outcome.verdict
        job.rationale = outcome.rationale
        job.strengths = outcome.strengths
        job.gaps = outcome.gaps
        job.red_flags = outcome.red_flags
        job.recommended_documents = list(outcome.recommended_documents)
        job.scored_at = datetime.now(UTC)
        stats.scored += 1

        if outcome.score >= settings.min_score and not outcome.red_flags:
            job.status = JobStatus.matched
            matched.append((outcome.score, job.id))
        else:
            job.status = JobStatus.scored
        session.add(job)
    session.commit()

    stats.matched = len(matched)
    # Best matches first, so a daily cap spends the budget on the strongest roles.
    matched.sort(reverse=True)
    remaining = _remaining_quota(session, settings)
    return [job_id for _score, job_id in matched[:remaining]]


def _remaining_quota(session: Session, settings: Settings) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    used = session.exec(
        select(func.count(Application.id)).where(Application.created_at >= since)
    ).one()
    return max(0, settings.max_new_applications_per_day - int(used or 0))


async def _build_applications(
    job_ids: list[int],
    profile: Profile,
    resume_text: str,
    settings: Settings,
    stats: RunStats,
) -> list[Application]:
    """Write the documents for each matched job and queue it for review."""
    queued: list[Application] = []
    if not job_ids:
        return queued

    async with build_http_client(settings) as client:
        for job_id in job_ids:
            with session_scope(settings) as session:
                job = session.get(Job, job_id)
                if job is None:
                    continue
                application = Application(job_id=job.id, status=ApplicationStatus.drafting)
                session.add(application)
                session.commit()
                application_id = application.id
                questions = await fetch_questions(client, job)

                try:
                    kinds = choose_document_kinds(job, profile)
                    documents = await asyncio.to_thread(
                        generate_documents, kinds, job, profile, resume_text, "", settings
                    )
                    answers = (
                        await asyncio.to_thread(
                            answer_screening_questions,
                            questions,
                            job,
                            profile,
                            resume_text,
                            settings,
                        )
                        if questions
                        else []
                    )
                except Exception as exc:  # noqa: BLE001 - one bad job must not stop the run
                    log.warning("Could not prepare application for job %s: %s", job_id, exc)
                    application.status = ApplicationStatus.failed
                    application.error = f"Document generation failed: {exc}"
                    session.add(application)
                    session.commit()
                    continue

                for document in documents:
                    session.add(
                        Document(
                            application_id=application_id,
                            kind=document.kind,
                            title=document.title,
                            content=document.content,
                        )
                    )
                    stats.documents += 1

                application.answers = [answer.model_dump() for answer in answers]
                application.status = ApplicationStatus.pending_review
                session.add(application)
                session.commit()
                queued.append(application)
                stats.queued += 1

    return queued


def _finish(run_id: int, settings: Settings, stats: RunStats, ok: bool, error: str = "") -> None:
    with session_scope(settings) as session:
        run = session.get(RunLog, run_id)
        if run is None:
            return
        run.finished_at = datetime.now(UTC)
        run.ok = ok
        run.stats = stats.as_dict()
        run.source_errors = stats.source_errors
        run.error = error
        session.add(run)


def _send_digest(queued: list[Application], stats: RunStats, settings: Settings) -> None:
    from jobsearch.notify.email import send_digest

    try:
        send_digest([app.id for app in queued], stats.as_dict(), settings)
    except Exception as exc:  # noqa: BLE001 - a failed email must not fail the run
        log.warning("Could not send the digest email: %s", exc)
