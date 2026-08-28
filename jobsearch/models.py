"""Database tables.

One SQLite file holds everything: discovered jobs, their fit assessment, the
applications awaiting your approval, and the generated documents.
"""

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, Index, Text
from sqlalchemy.types import JSON
from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(str, enum.Enum):
    discovered = "discovered"
    """Fetched from a source, not yet assessed."""
    filtered_out = "filtered_out"
    """Failed a hard preference filter. Kept for transparency, never scored."""
    scored = "scored"
    """Assessed but below your score threshold."""
    matched = "matched"
    """Above threshold; an Application row exists."""


class ApplicationStatus(str, enum.Enum):
    drafting = "drafting"
    """Documents are being generated."""
    pending_review = "pending_review"
    """Waiting for you in the dashboard."""
    approved = "approved"
    """You approved it; queued for submission."""
    submitting = "submitting"
    rejected = "rejected"
    """You skipped it."""
    submitted = "submitted"
    dry_run = "dry_run"
    """Would have submitted; SUBMIT_MODE=dry_run recorded the request instead."""
    needs_manual = "needs_manual"
    """No automated route. A packet is ready; you finish it in the browser."""
    failed = "failed"


class DocumentKind(str, enum.Enum):
    cover_letter = "cover_letter"
    statement_of_purpose = "statement_of_purpose"
    motivation_letter = "motivation_letter"
    screening_answers = "screening_answers"
    resume_summary = "resume_summary"


class ATS(str, enum.Enum):
    """Which applicant tracking system hosts the application form."""

    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    workable = "workable"
    unknown = "unknown"


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_status_score", "status", "score"),
        Index("ix_job_discovered_at", "discovered_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(index=True, unique=True)
    """Stable hash of company+title+location, so the same role is never queued twice."""

    source: str = Field(index=True)
    ats: ATS = Field(default=ATS.unknown)
    external_id: str = ""
    url: str = ""
    apply_url: str = ""

    company: str = ""
    title: str = ""
    location: str = ""
    remote_type: str = ""
    employment_type: str = ""
    salary_text: str = ""
    description: str = Field(default="", sa_column=Column(Text))
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utcnow)

    ats_meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    """Everything a submitter needs later: board token, posting id, form questions."""

    status: JobStatus = Field(default=JobStatus.discovered, index=True)
    filter_reason: str = ""

    score: int | None = Field(default=None, index=True)
    verdict: str = ""
    rationale: str = Field(default="", sa_column=Column(Text))
    strengths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    gaps: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    red_flags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    recommended_documents: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Document kinds the scorer judged this employer's process actually wants."""
    scored_at: datetime | None = None

    applications: list["Application"] = Relationship(back_populates="job")


class Application(SQLModel, table=True):
    __tablename__ = "application"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    status: ApplicationStatus = Field(default=ApplicationStatus.drafting, index=True)

    created_at: datetime = Field(default_factory=utcnow, index=True)
    reviewed_at: datetime | None = None
    submitted_at: datetime | None = None

    answers: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    """Screening question answers: [{question, answer, required, field_id}]."""

    submit_log: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    """Append-only audit trail of every submission attempt."""

    packet_dir: str = ""
    error: str = Field(default="", sa_column=Column(Text))
    note: str = Field(default="", sa_column=Column(Text))
    """Free-text instruction you leave when asking for a regeneration."""

    job: Job | None = Relationship(back_populates="applications")
    documents: list["Document"] = Relationship(
        back_populates="application",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Document(SQLModel, table=True):
    __tablename__ = "document"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: DocumentKind = Field(default=DocumentKind.cover_letter)
    title: str = ""
    content: str = Field(default="", sa_column=Column(Text))
    edited_by_user: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    pdf_path: str = ""
    docx_path: str = ""

    application: Application | None = Relationship(back_populates="documents")


class RunLog(SQLModel, table=True):
    __tablename__ = "run_log"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
    ok: bool = False
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_errors: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    error: str = Field(default="", sa_column=Column(Text))
