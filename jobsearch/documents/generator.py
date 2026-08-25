"""Generate the documents an application needs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from jobsearch.config import Settings, get_settings
from jobsearch.documents import prompts
from jobsearch.llm import generate_structured, generate_text
from jobsearch.models import DocumentKind, Job
from jobsearch.profile.loader import profile_brief
from jobsearch.profile.schema import Profile

log = logging.getLogger(__name__)

DESCRIPTION_BUDGET = 9000
WORD_COUNTS = {
    DocumentKind.cover_letter: lambda w: w.cover_letter_words,
    DocumentKind.statement_of_purpose: lambda w: w.statement_of_purpose_words,
    DocumentKind.motivation_letter: lambda w: w.cover_letter_words + 60,
    DocumentKind.resume_summary: lambda _w: 90,
}


@dataclass(slots=True)
class GeneratedDocument:
    kind: DocumentKind
    title: str
    content: str


class ScreeningAnswer(BaseModel):
    question: str
    answer: str
    needs_human: bool = Field(
        description="True when the answer is a guess the candidate must check before sending."
    )
    note: str = Field(default="", description="What is missing, when needs_human is true.")
    field_id: str = Field(default="", description="The form field this answers, when known.")


class ScreeningAnswerSet(BaseModel):
    answers: list[ScreeningAnswer]


def _grounding(profile: Profile) -> str:
    writing = profile.writing
    avoid = "; ".join(writing.avoid_phrases) or "none in particular"
    return prompts.GROUNDING.format(
        language=writing.language or "English",
        tone=writing.tone or "professional and direct",
        avoid=avoid,
    )


def _user_prompt(
    job: Job,
    profile: Profile,
    cv_text: str,
    task: str,
    note: str = "",
    include_fit: bool = True,
) -> str:
    description = (job.description or "").strip()
    if len(description) > DESCRIPTION_BUDGET:
        description = description[:DESCRIPTION_BUDGET] + "\n[...truncated...]"

    extra_bits = []
    if job.remote_type:
        extra_bits.append(f"Work mode: {job.remote_type}")
    if job.employment_type:
        extra_bits.append(f"Employment type: {job.employment_type.replace('_', '-')}")
    if job.salary_text:
        extra_bits.append(f"Salary: {job.salary_text}")
    if job.url:
        extra_bits.append(f"Posting: {job.url}")

    fit = ""
    if include_fit and job.rationale:
        strengths = "\n".join(f"- {s}" for s in (job.strengths or []))
        gaps = "\n".join(f"- {g}" for g in (job.gaps or []))
        fit = (
            "\n## Prior assessment of this match\n\n"
            f"{job.rationale}\n"
            + (f"\nStrongest evidence:\n{strengths}\n" if strengths else "")
            + (f"\nKnown gaps — address honestly or omit, never fake:\n{gaps}\n" if gaps else "")
        )

    note_block = (
        f"\n## The candidate's instructions for this draft\n\n{note.strip()}\n" if note else ""
    )

    return prompts.USER_TEMPLATE.format(
        profile=profile_brief(profile) or "(not provided)",
        cv=(cv_text or "(no CV file configured)").strip()[:16000],
        company=job.company,
        title=job.title,
        location=job.location or "not stated",
        extra="\n".join(extra_bits) + ("\n" if extra_bits else ""),
        description=description or "(no description provided by the source)",
        fit=fit,
        note=note_block,
        task=task,
    )


def generate_document(
    kind: DocumentKind,
    job: Job,
    profile: Profile,
    cv_text: str,
    note: str = "",
    settings: Settings | None = None,
) -> GeneratedDocument:
    """Write one document for one job."""
    settings = settings or get_settings()
    if kind not in prompts.SYSTEMS:
        raise ValueError(f"{kind} is not a writable document kind.")

    words = WORD_COUNTS[kind](profile.writing)
    system = prompts.SYSTEMS[kind].format(grounding=_grounding(profile), words=words)
    user = _user_prompt(job, profile, cv_text, prompts.TASKS[kind], note=note)

    content = generate_text(
        system,
        user,
        effort=settings.writing_effort,
        max_tokens=8000,
        settings=settings,
    )
    return GeneratedDocument(kind=kind, title=prompts.TITLES[kind], content=content)


def generate_documents(
    kinds: list[DocumentKind],
    job: Job,
    profile: Profile,
    cv_text: str,
    note: str = "",
    settings: Settings | None = None,
) -> list[GeneratedDocument]:
    """Write every requested document, skipping ones that fail."""
    documents: list[GeneratedDocument] = []
    for kind in kinds:
        try:
            documents.append(generate_document(kind, job, profile, cv_text, note, settings))
        except Exception as exc:  # noqa: BLE001 - one failed letter must not lose the rest
            log.warning("Could not write %s for %s: %s", kind.value, job.title, exc)
    return documents


def answer_screening_questions(
    questions: list[dict[str, Any]],
    job: Job,
    profile: Profile,
    cv_text: str,
    settings: Settings | None = None,
) -> list[ScreeningAnswer]:
    """Fill in an employer's application-form questions.

    `questions` uses the normalised shape produced by the ATS adapters:
    {"label", "required", "field_id", "type", "options": [...]}.
    """
    settings = settings or get_settings()
    if not questions:
        return []

    rendered = []
    for item in questions:
        line = f"- {item.get('label', '')}"
        if item.get("required"):
            line += " (required)"
        options = item.get("options") or []
        if options:
            line += "\n  Options (copy one exactly): " + " | ".join(str(o) for o in options)
        if item.get("field_id"):
            line += f"\n  field_id: {item['field_id']}"
        rendered.append(line)

    standard = profile.answers.model_dump()
    extra = standard.pop("extra", {}) or {}
    standard_lines = [
        f"- {key.replace('_', ' ')}: {value}" for key, value in standard.items() if value
    ]
    standard_lines += [f"- {key}: {value}" for key, value in extra.items() if value]

    task = (
        "Answer each of the employer's questions below.\n\n"
        "### The candidate's standard answers\n\n"
        + ("\n".join(standard_lines) or "(none provided)")
        + "\n\n### This employer's questions\n\n"
        + "\n".join(rendered)
        + "\n\nReturn one entry per question, echoing the question text and the "
        "field_id you were given."
    )

    result = generate_structured(
        prompts.SCREENING_SYSTEM,
        _user_prompt(job, profile, cv_text, task, include_fit=False),
        ScreeningAnswerSet,
        effort=settings.writing_effort,
        max_tokens=8000,
        settings=settings,
    )
    return result.answers


def choose_document_kinds(job: Job, profile: Profile) -> list[DocumentKind]:
    """Which documents to write for this posting.

    Uses the scorer's recommendation when it made one, and always falls back to
    a cover letter so an application is never sent bare.
    """
    del profile  # reserved: per-profile document policy
    recommended: list[DocumentKind] = []
    for name in job.recommended_documents or []:
        try:
            recommended.append(DocumentKind(name))
        except ValueError:
            continue
    writable = [k for k in recommended if k in prompts.SYSTEMS]
    return writable or [DocumentKind.cover_letter]
