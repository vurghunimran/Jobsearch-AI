"""Per-ATS submission recipes.

A recipe is *data*, not code: it says where an application form posts, what it
calls each field, and how to tell success from failure. Applicant-facing form
endpoints are not published API contracts and do change, so keeping them as
data means you fix a broken one by editing a dict — not by writing Python.

    HONEST STATUS
    -------------
    Every recipe below ships with `verified = False`. None of these endpoints
    was confirmed against a live board when this code was written, because the
    build environment had no network route to any job board.

    Before you set SUBMIT_MODE=live, verify each ATS you care about:

        jobsearch submit <application-id> --dry-run --show-request

    That prints the exact request that would be sent. Compare it against a real
    submission in your browser's network tab, correct the recipe if it differs,
    and set `verified = True` so `jobsearch doctor` stops warning about it.

An unverified recipe still works in dry-run mode, and any ATS without a recipe
falls through to a manual packet — so nothing silently fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobsearch.models import ATS


@dataclass(slots=True)
class SubmissionRecipe:
    ats: ATS
    endpoint: str
    """URL template. Formatted with the job's `ats_meta` dict."""

    field_map: dict[str, str]
    """canonical name -> the form field this ATS expects."""

    resume_field: str = "resume"
    answer_field_template: str = "{field_id}"
    """How a screening answer's field id becomes a form field name."""

    method: str = "POST"
    extra_fields: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    success_statuses: tuple[int, ...] = (200, 201, 202, 204)
    failure_markers: tuple[str, ...] = ()
    """Substrings that mean failure even on an HTTP 200."""
    verified: bool = False
    notes: str = ""


GREENHOUSE = SubmissionRecipe(
    ats=ATS.greenhouse,
    endpoint="https://boards.greenhouse.io/embed/job_app?token={job_id}",
    field_map={
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "phone": "phone",
        "cover_letter_text": "cover_letter_text",
    },
    resume_field="resume",
    answer_field_template="{field_id}",
    failure_markers=("error", "invalid", "captcha", "required field"),
    notes=(
        "Greenhouse's embedded application form. Custom questions arrive from the "
        "board API as field names like 'question_12345'; the answer's field_id is "
        "used verbatim. Boards with reCAPTCHA cannot be submitted this way and will "
        "fall back to a manual packet."
    ),
)

LEVER = SubmissionRecipe(
    ats=ATS.lever,
    endpoint="https://jobs.lever.co/{company}/{posting_id}/apply",
    field_map={
        "full_name": "name",
        "email": "email",
        "phone": "phone",
        "location": "location",
        "linkedin": "urls[LinkedIn]",
        "github": "urls[GitHub]",
        "portfolio": "urls[Portfolio]",
        "cover_letter_text": "comments",
    },
    resume_field="resume",
    answer_field_template="cards[{field_id}]",
    failure_markers=("error", "captcha"),
    notes=(
        "Lever's hosted apply form. Custom questions are grouped into 'cards'; the "
        "field_id from the posting is used as-is."
    ),
)

# Ashby and Workable are intentionally absent.
#
# Ashby's public posting API is read-only: submission goes through an internal
# GraphQL endpoint used by its own front end, and its documented
# `applicationForm.submit` needs the *employer's* API key, which an applicant
# does not have. Workable is the same story. Both therefore produce a manual
# packet — every document written, every field pre-filled for copy-paste, and a
# direct link — rather than a submitter that would quietly fail.
RECIPES: dict[ATS, SubmissionRecipe] = {
    ATS.greenhouse: GREENHOUSE,
    ATS.lever: LEVER,
}

MANUAL_ONLY_REASON = {
    ATS.ashby: "Ashby accepts applications only through its own front end or an employer API key.",
    ATS.workable: "Workable accepts applications only through its own front end.",
    ATS.unknown: "This posting is not on an ATS the agent can submit to directly.",
}


def recipe_for(ats: ATS) -> SubmissionRecipe | None:
    return RECIPES.get(ats)
