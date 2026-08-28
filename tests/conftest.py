"""Test fixtures: an isolated data directory and a fresh database per test."""

from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from jobsearch import db
    from jobsearch.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SUBMIT_MODE", "dry_run")
    monkeypatch.setenv("AUTO_SUBMIT_ON_APPROVAL", "false")
    monkeypatch.setenv("DASHBOARD_TOKEN", "")
    monkeypatch.setenv("MIN_SCORE", "70")
    # Settings and the engine are process-cached; drop both so each test is isolated.
    get_settings.cache_clear()
    db.reset_engine()

    resolved = get_settings()
    resolved.ensure_dirs()
    db.init_db(resolved)
    yield resolved

    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def profile(settings):
    """A filled-in profile with a CV on disk."""
    import yaml

    from jobsearch.profile.loader import load_profile, write_starter_profile

    write_starter_profile(settings, force=True)
    (settings.cv_dir / "cv.md").write_text(
        "Jane Rivera\nData analyst, 4 years.\n\n"
        "Bolt, Tallinn - Data Analyst (2022-2025). Built the churn model that cut "
        "monthly churn 18%. SQL, Python, dbt, Looker.\n",
        encoding="utf-8",
    )
    raw = yaml.safe_load(settings.profile_path.read_text())
    raw["identity"] = {
        "full_name": "Jane Rivera",
        "email": "jane@example.com",
        "phone": "+372 5555 0000",
        "city": "Tallinn",
        "country": "Estonia",
        "linkedin": "https://linkedin.com/in/janerivera",
    }
    raw["cv_file"] = "cv.md"
    raw["preferences"]["titles"] = ["data analyst", "analytics engineer"]
    raw["preferences"]["locations"] = ["Tallinn", "Estonia", "Berlin"]
    raw["preferences"]["exclude_keywords"] = ["security clearance"]
    raw["preferences"]["seniority"] = ["junior", "mid"]
    settings.profile_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_profile(settings)


@pytest.fixture
def seeded(settings, profile):
    """A matched job with an application, one document and two answers."""
    from jobsearch.db import session_scope
    from jobsearch.models import (
        ATS,
        Application,
        ApplicationStatus,
        Document,
        DocumentKind,
        Job,
        JobStatus,
    )

    with session_scope(settings) as session:
        job = Job(
            fingerprint="fp-test-1",
            source="greenhouse:examplecorp",
            ats=ATS.greenhouse,
            external_id="4567",
            url="https://boards.greenhouse.io/examplecorp/jobs/4567",
            apply_url="https://boards.greenhouse.io/examplecorp/jobs/4567",
            company="ExampleCorp",
            title="Data Analyst, Growth",
            location="Tallinn, Estonia",
            remote_type="hybrid",
            employment_type="full_time",
            description="Own growth reporting. SQL, dbt, experimentation.",
            posted_at=datetime.now(UTC),
            status=JobStatus.matched,
            score=88,
            verdict="strong",
            rationale="Four years of exactly this work.",
            strengths=["Churn model cut churn 18%"],
            gaps=["No experimentation platform work"],
            red_flags=[],
            recommended_documents=["cover_letter"],
            ats_meta={"board_token": "examplecorp", "job_id": "4567"},
            scored_at=datetime.now(UTC),
        )
        session.add(job)
        session.commit()

        application = Application(
            job_id=job.id,
            status=ApplicationStatus.pending_review,
            answers=[
                {
                    "question": "Why ExampleCorp?",
                    "answer": "Growth analytics is where I do my best work.",
                    "needs_human": False,
                    "note": "",
                    "field_id": "question_9001",
                },
                {
                    "question": "Expected salary?",
                    "answer": "EUR 55,000",
                    "needs_human": True,
                    "note": "Not in profile.",
                    "field_id": "question_9002",
                },
            ],
        )
        session.add(application)
        session.commit()
        session.add(
            Document(
                application_id=application.id,
                kind=DocumentKind.cover_letter,
                title="Cover letter",
                content="Dear ExampleCorp team,\n\nI owned growth reporting at Bolt.\n\nJane",
            )
        )
        session.commit()
        return {"job_id": job.id, "application_id": application.id}


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
