"""Fetch an employer's screening questions and normalise their shape."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from jobsearch.models import ATS, Job
from jobsearch.sources.greenhouse import fetch_questions as fetch_greenhouse_questions

log = logging.getLogger(__name__)

# Fields the recipe's field_map already fills in. Asking the model to answer
# "First Name" would be daft.
BUILTIN_FIELDS = {
    "first_name",
    "last_name",
    "name",
    "email",
    "phone",
    "resume",
    "resume_text",
    "cover_letter",
    "cover_letter_text",
    "location",
}


async def fetch_questions(client: httpx.AsyncClient, job: Job) -> list[dict[str, Any]]:
    """Return [{label, required, field_id, type, options}] for this posting.

    An empty list means "no extra questions, or we cannot see them" — either
    way the application proceeds on the standard fields.
    """
    meta = job.ats_meta or {}
    if job.ats is ATS.greenhouse and meta.get("board_token") and meta.get("job_id"):
        try:
            raw = await fetch_greenhouse_questions(client, meta["board_token"], meta["job_id"])
        except Exception as exc:  # noqa: BLE001 - questions are a nice-to-have
            log.warning("Could not fetch Greenhouse questions for job %s: %s", job.id, exc)
            return []
        return normalise_greenhouse(raw)
    return []


def normalise_greenhouse(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for entry in raw or []:
        label = (entry.get("label") or "").strip()
        required = bool(entry.get("required"))
        for form_field in entry.get("fields") or []:
            name = (form_field.get("name") or "").strip()
            if not name or name in BUILTIN_FIELDS:
                continue
            field_type = form_field.get("type") or ""
            if field_type == "input_file":
                # Extra file uploads (portfolio, transcript) cannot be answered
                # as text; they surface in the manual packet instead.
                continue
            options = [
                str(value.get("label"))
                for value in form_field.get("values") or []
                if isinstance(value, dict) and value.get("label") is not None
            ]
            questions.append(
                {
                    "label": label or name,
                    "required": required,
                    "field_id": name,
                    "type": field_type,
                    "options": options,
                }
            )
    return questions
