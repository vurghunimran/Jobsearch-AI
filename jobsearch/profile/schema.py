"""The single YAML file that describes you: CV, personal data, and what you want.

Everything the agent knows about you comes from `data/profile.yaml` plus the CV
file(s) it points at. Nothing here is ever sent anywhere except to Claude (to
write your documents) and to the ATS you approve an application for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Identity(BaseModel):
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    pronouns: str = ""

    def resolved_first_name(self) -> str:
        return self.first_name or (self.full_name.split(" ")[0] if self.full_name else "")

    def resolved_last_name(self) -> str:
        if self.last_name:
            return self.last_name
        parts = self.full_name.split(" ")
        return parts[-1] if len(parts) > 1 else ""


class WorkAuthorization(BaseModel):
    authorized_countries: list[str] = Field(default_factory=list)
    """Countries/regions where you can work without sponsorship, e.g. ["Estonia", "EU"]."""
    requires_sponsorship: bool = False
    current_visa_status: str = ""
    willing_to_relocate: bool = False
    relocation_targets: list[str] = Field(default_factory=list)
    notice_period: str = ""
    earliest_start_date: str = ""


class Education(BaseModel):
    degree: str = ""
    field_of_study: str = ""
    institution: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    grade: str = ""
    highlights: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Preferences(BaseModel):
    """The search itself. Every field here narrows what reaches your queue."""

    titles: list[str] = Field(default_factory=list)
    """Target job titles. Matched loosely against posting titles."""
    exclude_titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    """Optional: terms that should appear somewhere in the posting."""
    exclude_keywords: list[str] = Field(default_factory=list)
    """Hard veto. A posting containing any of these is dropped before scoring."""

    locations: list[str] = Field(default_factory=list)
    """Acceptable places, e.g. ["Tallinn", "Estonia", "Berlin", "Remote EU"]."""
    remote_types: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    accept_worldwide_remote: bool = True

    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    """full_time | part_time | contract | internship | temporary"""
    seniority: list[str] = Field(default_factory=list)
    """intern | junior | mid | senior | lead | manager — empty means no constraint."""

    min_salary: int | None = None
    salary_currency: str = "EUR"
    exclude_companies: list[str] = Field(default_factory=list)
    max_posting_age_days: int = 30

    must_not_require_sponsorship_free: bool = False
    """Drop postings that explicitly refuse to sponsor, when you need sponsorship."""


class SourceConfig(BaseModel):
    """Where to look. Company boards give the best signal; aggregators give reach."""

    greenhouse_boards: list[str] = Field(default_factory=list)
    """Greenhouse board tokens, e.g. "stripe" from job-boards.greenhouse.io/stripe."""
    lever_companies: list[str] = Field(default_factory=list)
    """Lever slugs, e.g. "netflix" from jobs.lever.co/netflix."""
    ashby_orgs: list[str] = Field(default_factory=list)
    """Ashby org names, e.g. "ramp" from jobs.ashbyhq.com/ramp."""
    workable_accounts: list[str] = Field(default_factory=list)
    """Workable subdomains, e.g. "acme" from apply.workable.com/acme."""

    remotive: bool = True
    arbeitnow: bool = True
    remoteok: bool = False
    adzuna: bool = False
    adzuna_country: str = "gb"
    """Adzuna country code: gb, us, de, nl, pl, at, ... Needs API credentials."""


class ApplicationAnswers(BaseModel):
    """Answers to the questions almost every application form asks."""

    years_of_experience: str = ""
    desired_salary: str = ""
    available_start_date: str = ""
    willing_to_relocate: str = ""
    requires_visa_sponsorship: str = ""
    authorized_to_work: str = ""
    how_did_you_hear: str = ""
    gender: str = ""
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""
    extra: dict[str, str] = Field(default_factory=dict)
    """Any other recurring question: {"Do you have a driver's licence?": "Yes"}."""


class WritingStyle(BaseModel):
    tone: str = "warm, direct, specific"
    cover_letter_words: int = 280
    statement_of_purpose_words: int = 550
    language: str = "English"
    avoid_phrases: list[str] = Field(
        default_factory=lambda: [
            "I am writing to express my interest",
            "I believe I would be a great fit",
            "passionate about leveraging synergies",
        ]
    )
    notes: str = ""
    """Anything else about how you want to sound."""


class Profile(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    headline: str = ""
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)

    cv_file: str = ""
    """Filename inside data/cv/ — PDF, DOCX, TXT or MD. Used as the attached resume."""
    extra_documents: dict[str, str] = Field(default_factory=dict)
    """Other attachables: {"transcript": "transcript.pdf"}."""

    preferences: Preferences = Field(default_factory=Preferences)
    sources: SourceConfig = Field(default_factory=SourceConfig)
    answers: ApplicationAnswers = Field(default_factory=ApplicationAnswers)
    writing: WritingStyle = Field(default_factory=WritingStyle)
