"""Build a ready-to-paste packet when no automated route exists.

Manual is a real outcome, not a failure. The packet holds every document as
PDF, DOCX and plain text, a copy of the CV, and a single file listing every
field value in the order a form asks for them — so finishing the application by
hand takes a minute rather than twenty.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from jobsearch.apply.base import ApplicationPacket, SubmitResult
from jobsearch.config import Settings, get_settings
from jobsearch.documents.render import render_docx, render_pdf
from jobsearch.matching.normalize import slugify

FIELD_LABELS = {
    "full_name": "Full name",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "phone": "Phone",
    "location": "Location",
    "linkedin": "LinkedIn",
    "github": "GitHub",
    "portfolio": "Portfolio",
}


def packet_dir(packet: ApplicationPacket, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    name = f"{stamp}-{slugify(packet.job.company)[:30]}-{slugify(packet.job.title)[:40]}"
    return settings.output_dir / f"{name}-{packet.application.id}"


def build_packet(
    packet: ApplicationPacket, reason: str, settings: Settings | None = None
) -> SubmitResult:
    """Write the packet to disk and return a needs_manual result pointing at it."""
    settings = settings or get_settings()
    directory = packet_dir(packet, settings)
    directory.mkdir(parents=True, exist_ok=True)

    author = packet.profile.identity.full_name
    written: list[str] = []
    for document in packet.documents:
        stem = document.kind.value
        (directory / f"{stem}.md").write_text(document.content, encoding="utf-8")
        try:
            render_pdf(document.content, directory / f"{stem}.pdf", document.title, author)
            render_docx(document.content, directory / f"{stem}.docx", document.title)
        except Exception as exc:  # noqa: BLE001 - the text version is the important one
            written.append(f"{stem} (PDF/DOCX rendering failed: {exc})")
        else:
            written.append(stem)

    if packet.resume_path and packet.resume_path.exists():
        shutil.copy2(packet.resume_path, directory / packet.resume_path.name)

    (directory / "APPLY.txt").write_text(_instructions(packet, reason), encoding="utf-8")

    return SubmitResult(
        "needs_manual",
        f"{reason} A packet is ready at {directory}.",
        request_preview={"packet_dir": str(directory), "documents": written},
    )


def _instructions(packet: ApplicationPacket, reason: str) -> str:
    job = packet.job
    fields = packet.canonical_fields()
    lines = [
        "HOW TO FINISH THIS APPLICATION",
        "=" * 60,
        "",
        f"Role:     {job.title}",
        f"Company:  {job.company}",
        f"Location: {job.location or 'not stated'}",
        f"Apply at: {job.apply_url or job.url}",
        "",
        f"Why this is manual: {reason}",
        "",
        "-" * 60,
        "FORM FIELDS",
        "-" * 60,
    ]
    for key, label in FIELD_LABELS.items():
        value = fields.get(key, "")
        if value:
            lines.append(f"{label + ':':<12} {value}")

    answers = packet.answers
    if answers:
        lines += ["", "-" * 60, "SCREENING QUESTIONS", "-" * 60]
        for item in answers:
            flag = "  <-- CHECK THIS, it is a guess" if item.get("needs_human") else ""
            lines.append("")
            lines.append(f"Q: {item.get('question', '')}{flag}")
            lines.append(f"A: {item.get('answer', '')}")
            if item.get("note"):
                lines.append(f"   note: {item['note']}")

    lines += ["", "-" * 60, "FILES IN THIS FOLDER", "-" * 60]
    for document in packet.documents:
        lines.append(f"- {document.kind.value}.pdf / .docx / .md  ({document.title})")
    if packet.resume_path:
        lines.append(f"- {packet.resume_path.name}  (your CV)")

    letter = packet.cover_letter_text()
    if letter:
        lines += [
            "",
            "-" * 60,
            "COVER LETTER (plain text, for paste-into-textarea forms)",
            "-" * 60,
            "",
            letter,
        ]
    return "\n".join(lines) + "\n"
