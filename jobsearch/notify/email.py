"""The daily email that tells you there is something to review."""

from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

from sqlmodel import select

from jobsearch.config import Settings, get_settings
from jobsearch.db import session_scope
from jobsearch.models import Application, Job

log = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    pass


def send_digest(
    application_ids: list[int], stats: dict[str, Any], settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    if not settings.email_configured():
        raise EmailNotConfigured(
            "SMTP_HOST, SMTP_FROM and DIGEST_TO must all be set to send the digest."
        )
    rows = _load(application_ids, settings)
    if not rows:
        return

    message = EmailMessage()
    message["Subject"] = _subject(rows)
    message["From"] = settings.smtp_from
    message["To"] = settings.digest_to
    message.set_content(_plain(rows, stats, settings))
    message.add_alternative(_html(rows, stats, settings), subtype="html")

    _send(message, settings)
    log.info("Digest sent to %s covering %d applications", settings.digest_to, len(rows))


def _send(message: EmailMessage, settings: Settings) -> None:
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            _login_and_send(server, message, settings)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_starttls:
            server.starttls()
        _login_and_send(server, message, settings)


def _login_and_send(server: smtplib.SMTP, message: EmailMessage, settings: Settings) -> None:
    if settings.smtp_username:
        server.login(settings.smtp_username, settings.smtp_password)
    server.send_message(message)


def _load(application_ids: list[int], settings: Settings) -> list[tuple[Application, Job]]:
    if not application_ids:
        return []
    with session_scope(settings) as session:
        applications = list(
            session.exec(select(Application).where(Application.id.in_(application_ids))).all()
        )
        jobs = {
            job.id: job
            for job in session.exec(
                select(Job).where(Job.id.in_([a.job_id for a in applications]))
            ).all()
        }
    rows = [(app, jobs[app.job_id]) for app in applications if app.job_id in jobs]
    rows.sort(key=lambda pair: pair[1].score or 0, reverse=True)
    return rows


def _link(settings: Settings, path: str = "/") -> str:
    """A dashboard URL that signs you in.

    The digest is usually opened on a phone that has never visited the
    dashboard, so a protected instance needs the token in the link itself.
    The dashboard swaps it for a cookie on arrival.
    """
    url = f"{settings.resolved_base_url}{path}"
    if settings.dashboard_token:
        url += f"?token={quote(settings.dashboard_token)}"
    return url


def _subject(rows: list[tuple[Application, Job]]) -> str:
    top = rows[0][1]
    if len(rows) == 1:
        return f"1 job to review: {top.title} at {top.company}"
    return f"{len(rows)} jobs to review — top match: {top.title} at {top.company}"


def _plain(rows: list[tuple[Application, Job]], stats: dict[str, Any], settings: Settings) -> str:
    lines = [
        f"{len(rows)} application(s) are drafted and waiting for your approval.",
        "",
        f"Review them at: {_link(settings)}",
        "",
    ]
    for application, job in rows:
        lines += [
            f"[{job.score}%] {job.title} — {job.company}",
            f"  {job.location or 'location not stated'} · "
            f"{job.remote_type or 'work mode not stated'}",
            f"  {job.rationale}",
            f"  Review: {_link(settings, f'/application/{application.id}')}",
            "",
        ]
    lines += [
        "---",
        f"Scanned {stats.get('fetched', 0)} postings, "
        f"{stats.get('new', 0)} new, "
        f"{stats.get('filtered_out', 0)} filtered out on your preferences, "
        f"{stats.get('scored', 0)} scored.",
    ]
    return "\n".join(lines)


def _html(rows: list[tuple[Application, Job]], stats: dict[str, Any], settings: Settings) -> str:
    cards = []
    for application, job in rows:
        score = job.score or 0
        colour = "#15803d" if score >= 85 else "#b45309" if score >= 70 else "#6b7280"
        meta = " · ".join(
            x for x in (job.location, job.remote_type, job.employment_type.replace("_", "-")) if x
        )
        cards.append(
            f"""
    <tr><td style="padding:0 0 14px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e5e7eb;border-radius:10px;">
        <tr><td style="padding:16px 18px;">
          <div style="font:600 12px/1 -apple-system,Segoe UI,Roboto,sans-serif;color:{colour};
                      letter-spacing:.04em;">{score}% FIT · {html.escape(job.verdict.upper())}</div>
          <div style="font:600 17px/1.35 -apple-system,Segoe UI,Roboto,sans-serif;color:#111827;
                      margin:7px 0 3px;">{html.escape(job.title)}</div>
          <div style="font:400 14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#4b5563;">
            {html.escape(job.company)}{" · " + html.escape(meta) if meta else ""}</div>
          <div style="font:400 14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#374151;
                      margin:10px 0 14px;">{html.escape(job.rationale)}</div>
          <a href="{_link(settings, f"/application/{application.id}")}"
             style="display:inline-block;background:#111827;color:#fff;text-decoration:none;
                    font:600 14px/1 -apple-system,Segoe UI,Roboto,sans-serif;
                    padding:10px 16px;border-radius:7px;">Review &amp; approve</a>
        </td></tr>
      </table>
    </td></tr>"""
        )

    summary = (
        f"Scanned {stats.get('fetched', 0)} postings · {stats.get('new', 0)} new · "
        f"{stats.get('filtered_out', 0)} filtered out · {stats.get('scored', 0)} scored"
    )
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px 12px;background:#f9fafb;">
<table align="center" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px;">
  <tr><td style="padding-bottom:18px;">
    <div style="font:600 20px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;color:#111827;">
      {len(rows)} application{"s" if len(rows) != 1 else ""} ready for your approval</div>
    <div style="font:400 14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#6b7280;
                margin-top:5px;">
      Documents are drafted. Nothing is sent until you approve it.</div>
  </td></tr>
  {"".join(cards)}
  <tr><td style="padding-top:8px;border-top:1px solid #e5e7eb;">
    <div style="font:400 12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#9ca3af;">
      {summary}<br><a href="{_link(settings)}" style="color:#6b7280;">Open the dashboard</a></div>
  </td></tr>
</table></body></html>"""
