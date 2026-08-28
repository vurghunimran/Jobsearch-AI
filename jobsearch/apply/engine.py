"""Turn a recipe plus a packet into an HTTP submission — or a preview of one."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from typing import Any

import httpx

from jobsearch.apply.base import ApplicationPacket, SubmitResult
from jobsearch.apply.recipes import SubmissionRecipe
from jobsearch.config import SubmitMode

log = logging.getLogger(__name__)

PREVIEW_VALUE_CHARS = 400


@dataclass(slots=True)
class BuiltRequest:
    url: str
    method: str
    data: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    resume_filename: str = ""
    resume_bytes: bytes = b""
    resume_content_type: str = "application/octet-stream"

    def preview(self) -> dict[str, Any]:
        """A loggable summary. Truncates long text, never includes file bytes."""
        fields = {
            key: (value[:PREVIEW_VALUE_CHARS] + "…" if len(value) > PREVIEW_VALUE_CHARS else value)
            for key, value in self.data.items()
        }
        summary: dict[str, Any] = {"method": self.method, "url": self.url, "fields": fields}
        if self.resume_filename:
            summary["file"] = {
                "field": "resume",
                "filename": self.resume_filename,
                "bytes": len(self.resume_bytes),
                "content_type": self.resume_content_type,
            }
        return summary

    def httpx_files(self, resume_field: str) -> dict[str, tuple[str, bytes, str]]:
        if not self.resume_filename:
            return {}
        return {resume_field: (self.resume_filename, self.resume_bytes, self.resume_content_type)}


class SubmissionBlocked(Exception):
    """A precondition failed. The application becomes a manual packet instead."""


def build_request(recipe: SubmissionRecipe, packet: ApplicationPacket) -> BuiltRequest:
    """Map the packet onto this ATS's field names."""
    meta = dict(packet.job.ats_meta or {})
    try:
        url = recipe.endpoint.format(**meta)
    except KeyError as exc:
        raise SubmissionBlocked(
            f"The posting is missing {exc} in its stored ATS metadata, so the "
            "application URL cannot be built."
        ) from exc

    canonical = packet.canonical_fields()
    data: dict[str, str] = {}
    for canonical_name, form_name in recipe.field_map.items():
        value = canonical.get(canonical_name, "")
        if value:
            data[form_name] = value
    data.update(recipe.extra_fields)

    for answer in packet.answers:
        field_id = str(answer.get("field_id") or "").strip()
        text = str(answer.get("answer") or "").strip()
        if not field_id or not text:
            continue
        data[recipe.answer_field_template.format(field_id=field_id)] = text

    built = BuiltRequest(url=url, method=recipe.method, data=data, headers=dict(recipe.headers))

    if packet.resume_path and packet.resume_path.exists():
        built.resume_filename = packet.resume_path.name
        built.resume_bytes = packet.resume_path.read_bytes()
        guessed, _ = mimetypes.guess_type(packet.resume_path.name)
        built.resume_content_type = guessed or "application/octet-stream"

    return built


def blockers(packet: ApplicationPacket) -> list[str]:
    """Reasons this application must not be sent automatically.

    Returned rather than raised, so a dry run can still show you the request it
    would build while telling you what would stop it going out for real.
    """
    reasons: list[str] = []
    ident = packet.profile.identity
    missing = [
        label
        for label, value in (("full name", ident.full_name), ("email", ident.email))
        if not value.strip()
    ]
    if missing:
        reasons.append(
            f"Your profile is missing {' and '.join(missing)}. Fill it in in data/profile.yaml."
        )

    unresolved = packet.unresolved_answers()
    if unresolved:
        questions = "; ".join(str(a.get("question", "?"))[:80] for a in unresolved[:3])
        reasons.append(
            f"{len(unresolved)} screening answer(s) are flagged as guesses and need your "
            f"confirmation first: {questions}"
        )

    if not packet.resume_path or not packet.resume_path.exists():
        reasons.append(
            "No CV file found. Set `cv_file` in data/profile.yaml and put the file in data/cv/."
        )
    return reasons


def _looks_like_failure(recipe: SubmissionRecipe, body: str) -> str | None:
    lowered = body.lower()
    for marker in recipe.failure_markers:
        if marker.lower() in lowered:
            return marker
    return None


async def submit(
    recipe: SubmissionRecipe,
    packet: ApplicationPacket,
    client: httpx.AsyncClient,
    mode: SubmitMode,
) -> SubmitResult:
    """Send the application, or describe exactly what would have been sent."""
    try:
        built = build_request(recipe, packet)
    except SubmissionBlocked as exc:
        return SubmitResult("needs_manual", str(exc))

    stoppers = blockers(packet)
    preview = built.preview()
    preview["ats"] = recipe.ats.value
    preview["recipe_verified"] = recipe.verified
    preview["blockers"] = stoppers

    if mode is SubmitMode.dry_run:
        detail = (
            f"Dry run — nothing was sent. Would POST {len(built.data)} fields "
            f"and {'a CV' if built.resume_filename else 'no CV'} to {built.url}."
        )
        if stoppers:
            detail += " A live send would be blocked: " + " ".join(stoppers)
        return SubmitResult("dry_run", detail, request_preview=preview)

    if stoppers:
        return SubmitResult("needs_manual", " ".join(stoppers), request_preview=preview)

    if not recipe.verified:
        return SubmitResult(
            "needs_manual",
            f"The {recipe.ats.value} recipe has not been verified against a live board yet. "
            "Run `jobsearch submit <id> --dry-run --show-request`, confirm the request "
            "matches a real submission, then set verified = True in jobsearch/apply/recipes.py.",
            request_preview=preview,
        )

    try:
        response = await client.request(
            built.method,
            built.url,
            data=built.data,
            files=built.httpx_files(recipe.resume_field),
            headers=built.headers,
        )
    except httpx.HTTPError as exc:
        return SubmitResult(
            "failed", f"Network error contacting {recipe.ats.value}: {exc}", preview
        )

    body = (response.text or "")[:4000]
    if response.status_code not in recipe.success_statuses:
        return SubmitResult(
            "failed",
            f"{recipe.ats.value} returned HTTP {response.status_code}.",
            preview,
            body,
        )

    marker = _looks_like_failure(recipe, body)
    if marker:
        return SubmitResult(
            "failed",
            f"{recipe.ats.value} returned HTTP {response.status_code} but the response "
            f"mentions '{marker}', so the application probably was not accepted.",
            preview,
            body,
        )

    return SubmitResult(
        "submitted",
        f"Submitted to {recipe.ats.value} (HTTP {response.status_code}).",
        preview,
        body,
    )
