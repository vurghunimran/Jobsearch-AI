"""Shared types for submitting an application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jobsearch.models import Application, Document, Job
from jobsearch.profile.schema import Profile

SubmitStatus = Literal["submitted", "dry_run", "needs_manual", "failed"]


@dataclass(slots=True)
class ApplicationPacket:
    """Everything needed to fill in one employer's form."""

    job: Job
    application: Application
    profile: Profile
    documents: list[Document]
    resume_path: Path | None = None
    answers: list[dict[str, Any]] = field(default_factory=list)

    def document(self, kind: str) -> Document | None:
        for doc in self.documents:
            if doc.kind.value == kind:
                return doc
        return None

    def cover_letter_text(self) -> str:
        """The letter body an ATS text field expects.

        Falls back through the document kinds so a statement of purpose is used
        when no cover letter was written.
        """
        for kind in (
            "cover_letter",
            "motivation_letter",
            "statement_of_purpose",
            "resume_summary",
        ):
            doc = self.document(kind)
            if doc and doc.content.strip():
                return doc.content.strip()
        return ""

    def canonical_fields(self) -> dict[str, str]:
        """Candidate details in a name-neutral form, mapped per-ATS by a recipe."""
        ident = self.profile.identity
        return {
            "first_name": ident.resolved_first_name(),
            "last_name": ident.resolved_last_name(),
            "full_name": ident.full_name,
            "email": ident.email,
            "phone": ident.phone,
            "location": ", ".join(x for x in (ident.city, ident.country) if x),
            "linkedin": ident.linkedin,
            "github": ident.github,
            "portfolio": ident.portfolio,
            "cover_letter_text": self.cover_letter_text(),
        }

    def unresolved_answers(self) -> list[dict[str, Any]]:
        """Answers the writer flagged as guesses. Block auto-submit on these."""
        return [a for a in self.answers if a.get("needs_human")]


@dataclass(slots=True)
class SubmitResult:
    status: SubmitStatus
    detail: str
    request_preview: dict[str, Any] = field(default_factory=dict)
    response_excerpt: str = ""

    def as_log_entry(self, mode: str) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "mode": mode,
            "request": self.request_preview,
            "response": self.response_excerpt[:2000],
        }
