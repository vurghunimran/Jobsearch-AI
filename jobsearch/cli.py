"""Command line interface."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import select

from jobsearch.config import SubmitMode, get_settings
from jobsearch.db import init_db, session_scope
from jobsearch.logging_setup import configure_logging
from jobsearch.models import Application, ApplicationStatus, Job, JobStatus

app = typer.Typer(
    add_completion=False,
    help="An AI agent that finds jobs, writes your documents, and applies once you approve.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    configure_logging(verbose)


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite an existing profile.yaml.")) -> None:
    """Create data/profile.yaml and the folders the agent needs."""
    from jobsearch.profile.loader import ProfileError, write_starter_profile

    settings = get_settings()
    init_db(settings)
    try:
        path = write_starter_profile(settings, force=force)
    except ProfileError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Created[/green] {path}")
    console.print(
        "\nNext:\n"
        f"  1. Put your CV in [bold]{settings.cv_dir}[/bold]\n"
        "     and set [bold]cv_file[/bold] to its filename.\n"
        f"  2. Fill in {path} — especially [bold]preferences[/bold] and [bold]sources[/bold].\n"
        "  3. Put OPENAI_API_KEY in [bold].env[/bold] (copy .env.example).\n"
        "  4. Run [bold]jobsearch doctor[/bold] to check everything is wired up."
    )


@app.command()
def doctor(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Check configuration, profile, CV, sources and submission recipes."""
    _setup_logging(verbose)
    from jobsearch.apply.recipes import RECIPES
    from jobsearch.profile.loader import ProfileError, cv_path, cv_text, load_profile
    from jobsearch.sources.registry import build_sources, check_all

    settings = get_settings()
    settings.ensure_dirs()
    problems = 0

    console.print("[bold]Configuration[/bold]")
    key = settings.openai_api_key
    console.print(f"  OPENAI_API_KEY     {'set' if key else '[red]MISSING[/red]'}")
    problems += 0 if key else 1
    console.print(f"  Writing model      {settings.model}")
    console.print(f"  Scoring model      {settings.scoring_model or settings.model + ' (same)'}")
    problems += _check_models(settings)
    console.print(f"  Database           {settings.resolved_database_url}")
    console.print(f"  Submit mode        {settings.submit_mode.value}")
    console.print(
        f"  Email digest       {'configured' if settings.email_configured() else 'not configured'}"
    )

    console.print("\n[bold]Profile[/bold]")
    try:
        profile = load_profile(settings)
    except ProfileError as exc:
        console.print(f"  [red]{exc}[/red]")
        raise typer.Exit(1) from exc

    ident = profile.identity
    for label, value in (("Name", ident.full_name), ("Email", ident.email)):
        console.print(f"  {label:<18} {value or '[red]MISSING[/red]'}")
        problems += 0 if value else 1

    path = cv_path(profile, settings)
    if path is None:
        console.print(
            "  CV                 [red]MISSING[/red] — set cv_file and add it to data/cv/"
        )
        problems += 1
    else:
        words = len(cv_text(profile, settings).split())
        console.print(f"  CV                 {path.name} ({words} words extracted)")
        if words < 80:
            console.print(
                "    [yellow]Very little text extracted — is this a scanned image?[/yellow]"
            )

    titles = profile.preferences.titles
    console.print(f"  Target titles      {', '.join(titles) if titles else '[red]NONE SET[/red]'}")
    problems += 0 if titles else 1
    console.print(f"  Locations          {', '.join(profile.preferences.locations) or 'anywhere'}")

    console.print("\n[bold]Sources[/bold]")
    sources = build_sources(profile, settings)
    if not sources:
        console.print(
            "  [red]No sources enabled.[/red] Add boards under `sources` in profile.yaml."
        )
        problems += 1
    else:
        results = asyncio.run(check_all(sources, settings))
        table = Table(show_header=True, header_style="bold")
        table.add_column("Source")
        table.add_column("Status")
        table.add_column("Postings", justify="right")
        table.add_column("Detail")
        for result in results:
            table.add_row(
                result.name,
                "[green]ok[/green]" if result.ok else "[red]failed[/red]",
                str(result.count),
                result.detail,
            )
            problems += 0 if result.ok else 1
        console.print(table)

    console.print("\n[bold]Submission recipes[/bold]")
    for ats, recipe in RECIPES.items():
        state = "[green]verified[/green]" if recipe.verified else "[yellow]unverified[/yellow]"
        console.print(f"  {ats.value:<12} {state}  {recipe.endpoint}")
    unverified = [r for r in RECIPES.values() if not r.verified]
    if unverified and settings.submit_mode is SubmitMode.live:
        console.print(
            "  [yellow]SUBMIT_MODE=live but some recipes are unverified. Those postings "
            "will fall back to manual packets until you verify them.[/yellow]"
        )

    console.print()
    if problems:
        console.print(
            f"[yellow]{problems} problem(s) to fix before the agent can run properly.[/yellow]"
        )
        raise typer.Exit(1)
    console.print("[green]Everything checks out.[/green]")


def _check_models(settings) -> int:
    """Confirm the configured models exist on this account.

    A wrong model id otherwise fails at 08:00 on the first real run, which is
    the worst possible moment to discover a typo.
    """
    from jobsearch.llm import LLMError, list_models

    if not settings.openai_api_key:
        return 0
    try:
        available = list_models(settings)
    except LLMError as exc:
        console.print(f"  Models             [red]could not list: {exc}[/red]")
        return 1

    problems = 0
    wanted = {"writing": settings.model}
    if settings.scoring_model:
        wanted["scoring"] = settings.scoring_model
    for role, name in wanted.items():
        if name in available:
            console.print(f"  {role.title()} model ok    [green]{name}[/green]")
        else:
            console.print(f"  {role.title()} model       [red]{name} is not available[/red]")
            problems += 1
    if problems:
        chat = [
            m
            for m in available
            if not any(
                x in m
                for x in ("embedding", "whisper", "tts", "dall-e", "moderation", "audio", "image")
            )
        ]
        console.print(f"  Available to you:  {', '.join(chat[:18]) or '(none listed)'}")
        console.print("  [yellow]Set MODEL in .env to one of the above.[/yellow]")
    return problems


@app.command()
def discover(
    notify: bool = typer.Option(True, help="Send the digest email when new matches are queued."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run one full search: find, filter, score, write, queue."""
    _setup_logging(verbose)
    from jobsearch.pipeline import run_discovery

    settings = get_settings()
    init_db(settings)
    stats, queued = asyncio.run(run_discovery(settings, notify=notify))

    table = Table(title="Search complete", show_header=False)
    for label, value in stats.as_dict().items():
        table.add_row(label.replace("_", " ").title(), str(value))
    console.print(table)
    for error in stats.source_errors:
        console.print(f"[yellow]{error['source']}: {error['error']}[/yellow]")
    if queued:
        console.print(
            f"\n[green]{len(queued)} application(s) awaiting your review[/green] at "
            f"{settings.resolved_base_url}/"
        )


@app.command(name="queue")
def show_queue() -> None:
    """List the applications waiting for your approval."""
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        applications = session.exec(
            select(Application)
            .where(Application.status == ApplicationStatus.pending_review)
            .order_by(Application.created_at.desc())
        ).all()
        jobs = {
            job.id: job
            for job in session.exec(
                select(Job).where(Job.id.in_([a.job_id for a in applications]))
            ).all()
        }

    if not applications:
        console.print("Nothing waiting for review.")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("ID", "Fit", "Role", "Company", "Where", "Apply via"):
        table.add_column(column)
    for application in sorted(applications, key=lambda a: jobs[a.job_id].score or 0, reverse=True):
        job = jobs[application.job_id]
        table.add_row(
            str(application.id),
            str(job.score or "—"),
            job.title,
            job.company,
            job.location or "—",
            job.ats.value,
        )
    console.print(table)


@app.command()
def submit(
    application_id: int,
    dry_run: bool = typer.Option(False, "--dry-run", help="Build the request but send nothing."),
    show_request: bool = typer.Option(
        False, "--show-request", help="Print the exact request that would be sent."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Submit one application, or preview exactly what would be sent."""
    _setup_logging(verbose)
    from jobsearch.apply.service import submit_application

    settings = get_settings()
    init_db(settings)
    mode = SubmitMode.dry_run if dry_run else settings.submit_mode

    async def run():
        with session_scope(settings) as session:
            application = session.get(Application, application_id)
            if application is None:
                console.print(f"[red]No application {application_id}.[/red]")
                raise typer.Exit(1)
            return await submit_application(session, application, settings=settings, mode=mode)

    result = asyncio.run(run())
    colour = {"submitted": "green", "dry_run": "cyan", "needs_manual": "yellow", "failed": "red"}
    console.print(f"[{colour.get(result.status, 'white')}]{result.status}[/] — {result.detail}")
    if show_request and result.request_preview:
        console.print_json(json.dumps(result.request_preview, default=str))


@app.command()
def approve(application_id: int, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Approve an application from the terminal and submit it."""
    _setup_logging(verbose)
    from jobsearch.apply.service import submit_application

    settings = get_settings()
    init_db(settings)

    async def run():
        with session_scope(settings) as session:
            application = session.get(Application, application_id)
            if application is None:
                console.print(f"[red]No application {application_id}.[/red]")
                raise typer.Exit(1)
            application.status = ApplicationStatus.approved
            session.add(application)
            session.commit()
            return await submit_application(session, application, settings=settings)

    result = asyncio.run(run())
    console.print(f"{result.status} — {result.detail}")


@app.command()
def digest() -> None:
    """Re-send the digest email for everything currently awaiting review."""
    from jobsearch.notify.email import EmailNotConfigured, send_digest

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        ids = [
            a.id
            for a in session.exec(
                select(Application).where(Application.status == ApplicationStatus.pending_review)
            ).all()
        ]
        pending_jobs = len(
            session.exec(select(Job.id).where(Job.status == JobStatus.matched)).all()
        )

    if not ids:
        console.print("Nothing awaiting review — no digest sent.")
        return
    try:
        send_digest(
            ids, {"fetched": 0, "new": 0, "filtered_out": 0, "scored": pending_jobs}, settings
        )
    except EmailNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Digest sent[/green] covering {len(ids)} application(s).")


@app.command()
def serve(
    host: str = typer.Option("", help="Overrides DASHBOARD_HOST."),
    port: int = typer.Option(0, help="Overrides DASHBOARD_PORT."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)."),
) -> None:
    """Start the review dashboard and the daily scheduler."""
    import uvicorn

    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings)
    uvicorn.run(
        "jobsearch.web.app:app",
        host=host or settings.dashboard_host,
        port=port or settings.dashboard_port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    app()
