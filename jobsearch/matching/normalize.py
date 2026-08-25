"""Turn the many ways job boards spell things into a few stable values."""

from __future__ import annotations

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table|section)>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_RE = re.compile(r"<li[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

EMPLOYMENT_TYPES = {
    "full_time": [
        "full time",
        "full-time",
        "fulltime",
        "permanent",
        "regular",
        "vollzeit",
        "cdi",
    ],
    "part_time": ["part time", "part-time", "parttime", "teilzeit"],
    "contract": ["contract", "contractor", "freelance", "b2b", "fixed term", "fixed-term", "cdd"],
    "internship": [
        "intern",
        "internship",
        "praktikum",
        "trainee",
        "co-op",
        "coop",
        "working student",
        "werkstudent",
    ],
    "temporary": ["temporary", "temp", "seasonal"],
}

SENIORITY_TERMS = {
    "intern": ["intern", "internship", "trainee", "working student", "werkstudent"],
    "junior": ["junior", "entry level", "entry-level", "graduate", "grad ", "associate i", "jr."],
    "mid": ["mid-level", "mid level", "intermediate", " ii", " 2"],
    "senior": ["senior", "sr.", "staff", "principal", "expert", " iii"],
    "lead": ["lead", "team lead", "tech lead", "architect"],
    "manager": ["manager", "head of", "director", "vp ", "chief", "cto", "ceo"],
}

REMOTE_TERMS = ["remote", "work from home", "wfh", "distributed", "anywhere", "telecommute"]
HYBRID_TERMS = [
    "hybrid",
    "flexible location",
    "partially remote",
    "2 days in office",
    "3 days in office",
]
ONSITE_TERMS = ["on-site", "on site", "onsite", "in office", "in-office"]


def strip_html(raw: str | None) -> str:
    """Convert an HTML job description to readable plain text.

    Job boards return wildly inconsistent markup; this keeps paragraph and
    bullet structure without pulling in a full HTML parser.
    """
    if not raw:
        return ""
    text = _BR_RE.sub("\n", raw)
    text = _LI_RE.sub("\n- ", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def slugify(value: str) -> str:
    """Lowercase ASCII slug used for fingerprints and loose comparisons."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def normalize_employment_type(*candidates: str | None) -> str:
    """Map free text to one of EMPLOYMENT_TYPES, or "" when nothing matches."""
    blob = " ".join(c.lower() for c in candidates if c)
    if not blob:
        return ""
    # Internship first: "Full-time internship" is an internship, not a full-time role.
    for key in ("internship", "contract", "part_time", "temporary", "full_time"):
        if any(term in blob for term in EMPLOYMENT_TYPES[key]):
            return key
    return ""


def detect_remote_type(*candidates: str | None) -> str:
    """Return "remote", "hybrid", "onsite", or "" when the posting doesn't say."""
    blob = " ".join(c.lower() for c in candidates if c)
    if not blob:
        return ""
    if any(term in blob for term in HYBRID_TERMS):
        return "hybrid"
    if any(term in blob for term in REMOTE_TERMS):
        return "remote"
    if any(term in blob for term in ONSITE_TERMS):
        return "onsite"
    return ""


def detect_seniority(title: str) -> str:
    """Infer seniority from a job title. Returns "" when it is not signalled."""
    lowered = f" {(title or '').lower()} "
    for level in ("manager", "lead", "senior", "intern", "junior", "mid"):
        if any(term in lowered for term in SENIORITY_TERMS[level]):
            return level
    return ""


def title_matches(title: str, wanted: list[str]) -> bool:
    """Loose title match: every word of a wanted phrase must appear in the title.

    "data analyst" matches "Senior Data Analyst, Growth" and "Analyst (Data)",
    but not "Data Engineer".
    """
    if not wanted:
        return True
    haystack = slugify(title).replace("-", " ")
    for phrase in wanted:
        words = [w for w in slugify(phrase).replace("-", " ").split() if w]
        if words and all(re.search(rf"\b{re.escape(w)}", haystack) for w in words):
            return True
    return False


def location_matches(
    location: str, remote_type: str, wanted: list[str], accept_worldwide_remote: bool
) -> bool:
    """Does this posting's location satisfy the user's location preferences?"""
    if not wanted:
        return True
    if remote_type == "remote" and accept_worldwide_remote:
        return True
    haystack = slugify(location).replace("-", " ")
    if not haystack:
        # Unstated location on a remote posting is common; keep it, drop otherwise.
        return remote_type == "remote" and accept_worldwide_remote
    for phrase in wanted:
        needle = slugify(phrase).replace("-", " ").strip()
        if needle and needle in haystack:
            return True
    return False


def contains_any(text: str, terms: list[str]) -> str | None:
    """Return the first term found in text, or None. Used for keyword vetoes."""
    if not terms:
        return None
    lowered = (text or "").lower()
    for term in terms:
        if term and term.lower() in lowered:
            return term
    return None
