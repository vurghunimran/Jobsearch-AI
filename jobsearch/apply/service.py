"""Orchestrates submitting one application: DB -> packet -> ATS -> DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from jobsearch.apply.base import ApplicationPacket, SubmitResult
from jobsearch.apply.engine import submit as engine_submit
from jobsearch.apply.manual import build_packet
from jobsearch.apply.recipes import MANUAL_ONLY_REASON, recipe_for
from jobsearch.config import Settings, SubmitMode, get_settings
from jobsearch.models import Application, ApplicationStatus, Document, Job
from jobsearch.profile.loader import cv_path, load_profile
from jobsearch.profile.schema import Profile
from jobsearch.sources.base import build_http_client

log = logging.getLogger(__name__)

STATUS_MAP = {
    "submitted": ApplicationStatus.submitted,
    "dry_run": ApplicationStatus.dry_run,
    "needs_manual": ApplicationStatus.needs_manual,
    "failed": ApplicationStatus.failed,
}


def load_packet(
    session: Session,
    application: Application,
    profile: Profile,
    settings: Settings | None = None,
) -> ApplicationPacket:
    settings = settings or get_settings()
    job = session.get(Job, application.job_id)
    if job is None:
        raise ValueError(f"Application {application.id} points at a job that no longer exists.")
    documents = list(
        session.exec(select(Document).where(Document.application_id == application.id)).all()
    )
    resume = cv_path(profile, settings)
    return ApplicationPacket(
        job=job,
        application=application,
        profile=profile,
        documents=documents,
        resume_path=Path(resume) if resume else None,
        answers=list(application.answers or []),
    )


async def submit_application(
    session: Session,
    application: Application,
    profile: Profile | None = None,
    settings: Settings | None = None,
    mode: SubmitMode | None = None,
) -> SubmitResult:
    """Submit one application and record the outcome.

    Always leaves the application in a state you can act on: submitted, a
    recorded dry run, or a manual packet on disk. Never a dead end.
    """
    settings = settings or get_settings()
    profile = profile or load_profile(settings)
    mode = mode or settings.submit_mode

    packet = load_packet(session, application, profile, settings)
    application.status = ApplicationStatus.submitting
    session.add(application)
    session.commit()

    recipe = recipe_for(packet.job.ats)
    if recipe is None:
        reason = MANUAL_ONLY_REASON.get(
            packet.job.ats, "No automated submission route for this posting."
        )
        result = build_packet(packet, reason, settings)
    else:
        async with build_http_client(settings) as client:
            result = await engine_submit(recipe, packet, client, mode)
        if result.status in {"needs_manual", "failed"}:
            # Give the user a working path forward even when automation stalls.
            manual = build_packet(packet, result.detail, settings)
            result = SubmitResult(
                status=result.status,
                detail=manual.detail,
                request_preview={**result.request_preview, **manual.request_preview},
                response_excerpt=result.response_excerpt,
            )

    _record(session, application, result, mode)
    return result


def _record(
    session: Session, application: Application, result: SubmitResult, mode: SubmitMode
) -> None:
    application.status = STATUS_MAP.get(result.status, ApplicationStatus.failed)
    application.submit_log = list(application.submit_log or []) + [
        {
            **result.as_log_entry(mode.value),
            "at": datetime.now(UTC).isoformat(),
        }
    ]
    if result.status == "submitted":
        application.submitted_at = datetime.now(UTC)
        application.error = ""
    elif result.status == "failed":
        application.error = result.detail
    else:
        application.error = ""

    packet_path = result.request_preview.get("packet_dir")
    if packet_path:
        application.packet_dir = str(packet_path)

    session.add(application)
    session.commit()
    log.info("Application %s -> %s: %s", application.id, result.status, result.detail)
