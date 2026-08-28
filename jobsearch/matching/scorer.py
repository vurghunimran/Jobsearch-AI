"""Model-backed fit scoring.

Runs on every posting that survives the hard filters. The score decides what
reaches your review queue; the rationale is shown next to it so you can tell
whether the agent understood the role.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from jobsearch.config import Settings, get_settings
from jobsearch.llm import LLMError, generate_structured
from jobsearch.models import Job
from jobsearch.profile.loader import profile_brief
from jobsearch.profile.schema import Profile

log = logging.getLogger(__name__)

DESCRIPTION_BUDGET = 6000
"""Characters of the job description sent to the scorer. Keeps cost predictable."""

DocumentKindName = Literal[
    "cover_letter", "statement_of_purpose", "motivation_letter", "resume_summary"
]


class JobFit(BaseModel):
    """The model's assessment of one posting against the candidate's profile."""

    score: int = Field(ge=0, le=100, description="Overall fit, 0-100.")
    verdict: Literal["strong", "good", "stretch", "poor"]
    rationale: str = Field(description="Two or three sentences explaining the score.")
    strengths: list[str] = Field(description="Concrete profile evidence that fits the role.")
    gaps: list[str] = Field(description="Requirements the candidate does not clearly meet.")
    red_flags: list[str] = Field(
        description="Reasons not to apply at all: wrong seniority, unusable location, "
        "disqualifying eligibility requirement. Empty when there are none."
    )
    recommended_documents: list[DocumentKindName] = Field(
        description="Which documents this employer's process actually calls for."
    )


SYSTEM = """\
You assess how well one candidate matches one job posting, for a job-search \
agent that applies on the candidate's behalf.

Score honestly. The candidate's time and reputation are spent on every \
application, so an inflated score is worse than a low one. Anchor the scale:

- 85-100 (strong): meets essentially every stated requirement; a recruiter \
would shortlist this.
- 70-84 (good): meets the core requirements with a minor gap or two that a \
cover letter can address.
- 50-69 (stretch): plausible but missing something material — years of \
experience, a required qualification, a core technology.
- 0-49 (poor): wrong role, wrong level, or the candidate is ineligible.

Judge against what the posting actually requires, not against how impressive \
the company is. Treat "nice to have" as optional. If the posting states an \
eligibility requirement the candidate cannot meet (work authorization, a \
licence, on-site presence in a place they cannot be), that is a red flag and \
caps the score below 50 regardless of skills match.

Base every strength on evidence in the CV or profile. Never invent experience.\
"""

USER_TEMPLATE = """\
## Candidate profile

{profile}

## Candidate CV

{cv}

## Job posting

Title: {title}
Company: {company}
Location: {location}{remote}
Employment type: {employment_type}
{salary}
Description:
{description}

## Task

Assess this candidate against this posting. Return the structured assessment.
For `recommended_documents`, pick what this specific employer's process calls \
for: a cover letter for most roles; a statement of purpose for academic, \
research or fellowship postings; a motivation letter where the posting or \
region uses that convention; `resume_summary` when the posting asks for a \
short profile blurb. Return an empty list only if the posting explicitly says \
not to send any."""


def build_prompt(job: Job, profile: Profile, cv_text: str) -> str:
    description = (job.description or "").strip()
    if len(description) > DESCRIPTION_BUDGET:
        description = description[:DESCRIPTION_BUDGET] + "\n[...truncated...]"
    return USER_TEMPLATE.format(
        profile=profile_brief(profile) or "(not provided)",
        cv=(cv_text or "(no CV file configured)").strip()[:12000],
        title=job.title,
        company=job.company,
        location=job.location or "not stated",
        remote=f" ({job.remote_type})" if job.remote_type else "",
        employment_type=job.employment_type or "not stated",
        salary=f"Salary: {job.salary_text}\n" if job.salary_text else "",
        description=description or "(no description provided by the source)",
    )


def score_job(job: Job, profile: Profile, cv_text: str, settings: Settings | None = None) -> JobFit:
    settings = settings or get_settings()
    return generate_structured(
        SYSTEM,
        build_prompt(job, profile, cv_text),
        JobFit,
        effort=settings.scoring_effort,
        max_tokens=4000,
        settings=settings,
    )


def score_jobs(
    jobs: list[Job],
    profile: Profile,
    cv_text: str,
    settings: Settings | None = None,
    max_workers: int = 6,
) -> dict[int, JobFit | Exception]:
    """Score many postings in parallel.

    Returns a result per job id — a JobFit, or the exception that scoring hit.
    A single failure never aborts the batch; the pipeline logs it and moves on.
    """
    settings = settings or get_settings()
    results: dict[int, JobFit | Exception] = {}
    if not jobs:
        return results

    def run(job: Job) -> tuple[int, JobFit | Exception]:
        try:
            return job.id, score_job(job, profile, cv_text, settings)
        except LLMError as exc:
            log.warning("Scoring failed for %s at %s: %s", job.title, job.company, exc)
            return job.id, exc
        except Exception as exc:  # noqa: BLE001 - one bad posting must not stop the run
            log.exception("Unexpected scoring error for job %s", job.id)
            return job.id, exc

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for job_id, outcome in pool.map(run, jobs):
            results[job_id] = outcome
    return results
