"""The review dashboard.

Deliberately plain: server-rendered HTML and ordinary form posts. It runs on
your machine, holds your CV and personal data, and has no third-party assets —
so there is nothing to leak and nothing to break when you are offline.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from jobsearch.apply.service import submit_application
from jobsearch.config import Settings, get_settings
from jobsearch.db import get_engine, init_db, session_scope
from jobsearch.documents.render import to_html
from jobsearch.models import (
    Application,
    ApplicationStatus,
    Document,
    DocumentKind,
    Job,
    JobStatus,
    RunLog,
)
from jobsearch.profile.loader import cv_text, load_profile

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

OPEN_STATUSES = [ApplicationStatus.pending_review, ApplicationStatus.drafting]
STATUS_LABELS = {
    ApplicationStatus.drafting: ("Drafting", "muted"),
    ApplicationStatus.pending_review: ("Awaiting your review", "pending"),
    ApplicationStatus.approved: ("Approved", "ok"),
    ApplicationStatus.submitting: ("Submitting", "pending"),
    ApplicationStatus.submitted: ("Submitted", "ok"),
    ApplicationStatus.dry_run: ("Dry run recorded", "muted"),
    ApplicationStatus.needs_manual: ("Needs you to finish", "warn"),
    ApplicationStatus.rejected: ("Skipped", "muted"),
    ApplicationStatus.failed: ("Failed", "bad"),
}


def get_session() -> Session:
    with session_scope() as session:
        yield session


# Reachable without a token: the container health check, and static assets
# (which the browser fetches without the query string).
OPEN_PREFIXES = ("/healthz", "/static")

TOKEN_COOKIE = "jobsearch_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings)
    scheduler = None
    try:
        from jobsearch.scheduler import start_scheduler

        scheduler = start_scheduler(settings)
        app.state.scheduler = scheduler
    except Exception as exc:  # noqa: BLE001 - the dashboard is useful without a scheduler
        log.warning("Scheduler did not start: %s", exc)
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="jobsearch-ai", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def token_gate(request: Request, call_next):
        """Shared-token gate. Disabled when DASHBOARD_TOKEN is unset.

        A valid `?token=` is exchanged for a cookie, so a link tapped in the
        digest email signs you in and every link from there on just works.
        """
        if not settings.dashboard_token or request.url.path.startswith(OPEN_PREFIXES):
            return await call_next(request)

        from_query = request.query_params.get("token")
        supplied = from_query or request.cookies.get(TOKEN_COOKIE)
        if supplied != settings.dashboard_token:
            return HTMLResponse(
                "<h1>401</h1><p>This dashboard needs a token. Open it from the link "
                "in your digest email, or add <code>?token=...</code> to the URL.</p>",
                status_code=401,
            )

        response = await call_next(request)
        if from_query == settings.dashboard_token:
            response.set_cookie(
                TOKEN_COOKIE,
                settings.dashboard_token,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                # Only send the cookie over TLS when the dashboard is served over TLS.
                secure=settings.resolved_base_url.startswith("https://"),
            )
        return response

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        counts = _counts(context.get("session"))
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={"settings": settings, "counts": counts, **context},
        )

    # ---------------------------------------------------------------- views

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, session: Session = Depends(get_session)):
        applications = session.exec(
            select(Application)
            .where(Application.status.in_(OPEN_STATUSES))
            .order_by(Application.created_at.desc())
        ).all()
        rows = _decorate(session, applications)
        rows.sort(key=lambda row: row["job"].score or 0, reverse=True)
        last_run = session.exec(select(RunLog).order_by(RunLog.started_at.desc())).first()
        return render(request, "queue.html", session=session, rows=rows, last_run=last_run)

    @app.get(
        "/application/{application_id}",
        response_class=HTMLResponse,
    )
    def detail(request: Request, application_id: int, session: Session = Depends(get_session)):
        application = _get(session, application_id)
        job = session.get(Job, application.job_id)
        documents = session.exec(
            select(Document).where(Document.application_id == application_id)
        ).all()
        return render(
            request,
            "application.html",
            session=session,
            application=application,
            job=job,
            documents=list(documents),
            status=STATUS_LABELS.get(application.status, (application.status.value, "muted")),
            to_html=to_html,
        )

    @app.get("/applications", response_class=HTMLResponse)
    def history(request: Request, status: str = "", session: Session = Depends(get_session)):
        query = select(Application).order_by(Application.created_at.desc())
        if status:
            query = query.where(Application.status == status)
        rows = _decorate(session, session.exec(query.limit(300)).all())
        return render(
            request,
            "applications.html",
            session=session,
            rows=rows,
            status=status,
            labels=STATUS_LABELS,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, show: str = "all", session: Session = Depends(get_session)):
        query = select(Job).order_by(Job.discovered_at.desc())
        if show == "filtered":
            query = query.where(Job.status == JobStatus.filtered_out)
        elif show == "scored":
            query = query.where(Job.status == JobStatus.scored)
        return render(
            request,
            "jobs.html",
            session=session,
            jobs=list(session.exec(query.limit(400)).all()),
            show=show,
        )

    # -------------------------------------------------------------- actions

    @app.post("/application/{application_id}/approve")
    async def approve(application_id: int):
        with session_scope(settings) as session:
            application = _get(session, application_id)
            application.status = ApplicationStatus.approved
            application.reviewed_at = datetime.now(UTC)
            session.add(application)
        if settings.auto_submit_on_approval:
            asyncio.create_task(_submit_later(application_id, settings))
        return _back(application_id)

    @app.post("/application/{application_id}/reject")
    def reject(application_id: int, session: Session = Depends(get_session)):
        application = _get(session, application_id)
        application.status = ApplicationStatus.rejected
        application.reviewed_at = datetime.now(UTC)
        session.add(application)
        return RedirectResponse("/", status_code=303)

    @app.post("/application/{application_id}/submit")
    async def submit_now(application_id: int):
        with session_scope(settings) as session:
            application = _get(session, application_id)
            await submit_application(session, application, settings=settings)
        return _back(application_id)

    @app.post(
        "/application/{application_id}/document/{document_id}",
    )
    def save_document(
        application_id: int,
        document_id: int,
        content: str = Form(...),
        session: Session = Depends(get_session),
    ):
        document = session.get(Document, document_id)
        if document is None or document.application_id != application_id:
            raise HTTPException(404, "No such document on this application.")
        document.content = content
        document.edited_by_user = True
        document.updated_at = datetime.now(UTC)
        session.add(document)
        return _back(application_id)

    @app.post("/application/{application_id}/answers")
    async def save_answers(request: Request, application_id: int):
        form = await request.form()
        with session_scope(settings) as session:
            application = _get(session, application_id)
            # Deep copy: mutating the dicts in place would also mutate the
            # session's committed state, and SQLAlchemy would see no change.
            answers = deepcopy(list(application.answers or []))
            for index, answer in enumerate(answers):
                submitted = form.get(f"answer_{index}")
                if submitted is not None:
                    answer["answer"] = str(submitted)
                    # Editing an answer is the confirmation the writer asked for.
                    answer["needs_human"] = False
            application.answers = answers
            session.add(application)
        return _back(application_id)

    @app.post("/application/{application_id}/regenerate")
    async def regenerate(application_id: int, note: str = Form(""), kind: str = Form("")):
        await asyncio.to_thread(_regenerate, application_id, note, kind, settings)
        return _back(application_id)

    @app.post("/run")
    async def run_now():
        asyncio.create_task(_run_discovery(settings))
        return RedirectResponse("/", status_code=303)

    @app.get("/healthz")
    def healthz():
        try:
            with Session(get_engine(settings)) as session:
                session.exec(select(Job.id).limit(1)).first()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return {"ok": True}

    return app


# -------------------------------------------------------------------- helpers


def _get(session: Session, application_id: int) -> Application:
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, f"No application {application_id}.")
    return application


def _back(application_id: int) -> RedirectResponse:
    return RedirectResponse(f"/application/{application_id}", status_code=303)


def _decorate(session: Session, applications: list[Application]) -> list[dict[str, Any]]:
    """Attach each application's job and document count in two queries, not 2N."""
    applications = list(applications)
    if not applications:
        return []
    jobs = {
        job.id: job
        for job in session.exec(
            select(Job).where(Job.id.in_([a.job_id for a in applications]))
        ).all()
    }
    documents: dict[int, int] = {}
    for document in session.exec(
        select(Document).where(Document.application_id.in_([a.id for a in applications]))
    ).all():
        documents[document.application_id] = documents.get(document.application_id, 0) + 1

    rows = []
    for application in applications:
        job = jobs.get(application.job_id)
        if job is None:
            continue
        rows.append(
            {
                "application": application,
                "job": job,
                "documents": documents.get(application.id, 0),
                "status": STATUS_LABELS.get(
                    application.status, (application.status.value, "muted")
                ),
            }
        )
    return rows


def _counts(session: Session | None) -> dict[str, int]:
    if session is None:
        return {}
    since = datetime.now(UTC) - timedelta(days=7)
    pending = len(
        session.exec(select(Application.id).where(Application.status.in_(OPEN_STATUSES))).all()
    )
    recent = len(session.exec(select(Application.id).where(Application.created_at >= since)).all())
    return {"pending": pending, "recent": recent}


async def _submit_later(application_id: int, settings: Settings) -> None:
    try:
        with session_scope(settings) as session:
            application = session.get(Application, application_id)
            if application is None or application.status is not ApplicationStatus.approved:
                return
            await submit_application(session, application, settings=settings)
    except Exception:  # noqa: BLE001
        log.exception("Background submission failed for application %s", application_id)


async def _run_discovery(settings: Settings) -> None:
    from jobsearch.pipeline import run_discovery

    try:
        await run_discovery(settings)
    except Exception:  # noqa: BLE001
        log.exception("Manual discovery run failed")


def _regenerate(application_id: int, note: str, kind: str, settings: Settings) -> None:
    """Rewrite this application's documents, optionally with an instruction."""
    from jobsearch.documents.generator import choose_document_kinds, generate_documents

    profile = load_profile(settings)
    resume_text = cv_text(profile, settings)
    with session_scope(settings) as session:
        application = session.get(Application, application_id)
        if application is None:
            return
        job = session.get(Job, application.job_id)
        if job is None:
            return

        if kind:
            try:
                kinds = [DocumentKind(kind)]
            except ValueError:
                kinds = choose_document_kinds(job, profile)
        else:
            existing = session.exec(
                select(Document).where(Document.application_id == application_id)
            ).all()
            kinds = [d.kind for d in existing] or choose_document_kinds(job, profile)

        documents = generate_documents(kinds, job, profile, resume_text, note, settings)
        by_kind = {
            d.kind: d
            for d in session.exec(
                select(Document).where(Document.application_id == application_id)
            ).all()
        }
        for generated in documents:
            existing_doc = by_kind.get(generated.kind)
            if existing_doc is None:
                session.add(
                    Document(
                        application_id=application_id,
                        kind=generated.kind,
                        title=generated.title,
                        content=generated.content,
                    )
                )
            else:
                existing_doc.content = generated.content
                existing_doc.edited_by_user = False
                existing_doc.updated_at = datetime.now(UTC)
                session.add(existing_doc)

        application.note = note
        if application.status is ApplicationStatus.failed:
            application.status = ApplicationStatus.pending_review
            application.error = ""
        session.add(application)


app = create_app()
