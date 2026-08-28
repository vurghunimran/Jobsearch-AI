"""End-to-end pipeline: discovery -> filter -> score -> documents -> queue.

The model and the network are stubbed; everything else is the real code path.
"""

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from jobsearch.db import session_scope
from jobsearch.documents.generator import GeneratedDocument, ScreeningAnswer
from jobsearch.matching.scorer import JobFit
from jobsearch.models import (
    Application,
    ApplicationStatus,
    Document,
    DocumentKind,
    Job,
    JobStatus,
    RunLog,
)
from jobsearch.sources.base import RawJob


def posting(title, company, **overrides) -> RawJob:
    base = {
        "source": "test",
        "external_id": company.lower(),
        "title": title,
        "company": company,
        "url": f"https://example.com/{company}",
        "description": "SQL, Python and dbt.",
        "location": "Tallinn, Estonia",
        "employment_type": "full_time",
        "posted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return RawJob(**base)


def fit(score: int, red_flags=None) -> JobFit:
    return JobFit(
        score=score,
        verdict="strong" if score >= 85 else "good" if score >= 70 else "poor",
        rationale=f"Scored {score}.",
        strengths=["Relevant experience"],
        gaps=[],
        red_flags=red_flags or [],
        recommended_documents=["cover_letter"],
    )


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the network and every model call with deterministic stubs."""
    state = {"raw": [], "scores": {}, "documents": 0, "answers": []}

    async def fake_fetch_all(sources, settings=None):
        return state["raw"], []

    def fake_score_jobs(jobs, profile, cv, settings=None, max_workers=6):
        return {job.id: state["scores"].get(job.title, fit(90)) for job in jobs}

    def fake_generate(kinds, job, profile, cv, note="", settings=None):
        state["documents"] += 1
        return [
            GeneratedDocument(
                kind, kind.value.replace("_", " ").title(), f"Letter for {job.title}."
            )
            for kind in kinds
        ]

    async def fake_questions(client, job):
        return [{"label": "Why us?", "required": True, "field_id": "question_1", "options": []}]

    def fake_answers(questions, job, profile, cv, settings=None):
        return state["answers"] or [
            ScreeningAnswer(
                question="Why us?", answer="Because.", needs_human=False, field_id="question_1"
            )
        ]

    monkeypatch.setattr("jobsearch.pipeline.fetch_all", fake_fetch_all)
    monkeypatch.setattr("jobsearch.pipeline.score_jobs", fake_score_jobs)
    monkeypatch.setattr("jobsearch.pipeline.generate_documents", fake_generate)
    monkeypatch.setattr("jobsearch.pipeline.fetch_questions", fake_questions)
    monkeypatch.setattr("jobsearch.pipeline.answer_screening_questions", fake_answers)
    return state


async def run(settings):
    from jobsearch.pipeline import run_discovery

    return await run_discovery(settings, notify=False)


class TestHappyPath:
    async def test_a_match_becomes_a_reviewable_application(self, settings, profile, stub_pipeline):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        stats, queued = await run(settings)

        assert stats.fetched == 1 and stats.new == 1
        assert stats.scored == 1 and stats.matched == 1 and stats.queued == 1
        assert len(queued) == 1

        with session_scope(settings) as session:
            application = session.exec(select(Application)).one()
            assert application.status is ApplicationStatus.pending_review
            assert application.answers[0]["field_id"] == "question_1"
            document = session.exec(select(Document)).one()
            assert document.kind is DocumentKind.cover_letter
            assert "Data Analyst" in document.content
            job = session.exec(select(Job)).one()
            assert job.status is JobStatus.matched and job.score == 90

    async def test_the_run_is_logged(self, settings, profile, stub_pipeline):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        await run(settings)
        with session_scope(settings) as session:
            log = session.exec(select(RunLog)).one()
            assert log.ok and log.finished_at is not None
            assert log.stats["queued"] == 1


class TestFilteringAndScoring:
    async def test_filtered_postings_never_reach_the_scorer(self, settings, profile, stub_pipeline):
        stub_pipeline["raw"] = [
            posting("Data Analyst", "AlphaCo"),
            posting("Data Engineer", "BetaCo"),  # wrong title
            posting("Data Analyst", "GammaCo", location="Tokyo, Japan"),  # wrong place
        ]
        stats, _ = await run(settings)
        assert stats.filtered_out == 2
        assert stats.scored == 1

        with session_scope(settings) as session:
            rejected = session.exec(select(Job).where(Job.status == JobStatus.filtered_out)).all()
            # Every rejection keeps the reason, so the user can tune preferences.
            assert all(job.filter_reason for job in rejected)

    async def test_a_low_score_is_kept_but_not_queued(self, settings, profile, stub_pipeline):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        stub_pipeline["scores"] = {"Data Analyst": fit(45)}
        stats, queued = await run(settings)

        assert stats.scored == 1 and stats.matched == 0 and not queued
        with session_scope(settings) as session:
            assert session.exec(select(Job)).one().status is JobStatus.scored
            assert session.exec(select(Application)).all() == []

    async def test_a_red_flag_blocks_a_high_score(self, settings, profile, stub_pipeline):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        stub_pipeline["scores"] = {"Data Analyst": fit(95, ["Requires US citizenship"])}
        stats, queued = await run(settings)
        assert stats.matched == 0 and not queued


class TestDeduplicationAndQuotas:
    async def test_a_second_run_does_not_requeue_the_same_role(
        self, settings, profile, stub_pipeline
    ):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        await run(settings)
        stats, queued = await run(settings)

        assert stats.new == 0 and not queued
        with session_scope(settings) as session:
            assert len(session.exec(select(Job)).all()) == 1
            assert len(session.exec(select(Application)).all()) == 1

    async def test_the_daily_cap_keeps_the_highest_scoring_roles(
        self, settings, profile, stub_pipeline, monkeypatch
    ):
        monkeypatch.setattr(settings, "max_new_applications_per_day", 2)
        stub_pipeline["raw"] = [
            posting("Data Analyst", "LowCo"),
            posting("Data Analyst", "MidCo"),
            posting("Data Analyst", "TopCo"),
        ]
        stub_pipeline["scores"] = {"Data Analyst": fit(90)}

        # Same title everywhere, so vary the score by company instead.
        def by_company(jobs, profile, cv, settings=None, max_workers=6):
            table = {"LowCo": 72, "MidCo": 84, "TopCo": 97}
            return {job.id: fit(table[job.company]) for job in jobs}

        monkeypatch.setattr("jobsearch.pipeline.score_jobs", by_company)
        stats, queued = await run(settings)

        assert stats.matched == 3, "all three clear the threshold"
        assert stats.queued == 2, "but the daily cap only allows two"
        with session_scope(settings) as session:
            queued_companies = {
                session.get(Job, a.job_id).company for a in session.exec(select(Application)).all()
            }
        assert queued_companies == {"TopCo", "MidCo"}


class TestResilience:
    async def test_a_failed_document_run_marks_that_application_only(
        self, settings, profile, stub_pipeline, monkeypatch
    ):
        stub_pipeline["raw"] = [
            posting("Data Analyst", "AlphaCo"),
            posting("Data Analyst", "BetaCo"),
        ]

        def explode_for_alpha(kinds, job, profile, cv, note="", settings=None):
            if job.company == "AlphaCo":
                raise RuntimeError("the model provider is down")
            return [GeneratedDocument(DocumentKind.cover_letter, "Cover letter", "Body.")]

        monkeypatch.setattr("jobsearch.pipeline.generate_documents", explode_for_alpha)
        stats, queued = await run(settings)

        assert stats.queued == 1, "the healthy application still goes through"
        with session_scope(settings) as session:
            statuses = {
                session.get(Job, a.job_id).company: a.status
                for a in session.exec(select(Application)).all()
            }
        assert statuses["AlphaCo"] is ApplicationStatus.failed
        assert statuses["BetaCo"] is ApplicationStatus.pending_review

    async def test_a_scoring_error_is_counted_not_fatal(
        self, settings, profile, stub_pipeline, monkeypatch
    ):
        stub_pipeline["raw"] = [posting("Data Analyst", "AlphaCo")]
        monkeypatch.setattr(
            "jobsearch.pipeline.score_jobs",
            lambda jobs, p, cv, s=None, max_workers=6: {
                j.id: RuntimeError("rate limited") for j in jobs
            },
        )
        stats, queued = await run(settings)
        assert stats.score_errors == 1 and stats.matched == 0 and not queued

    async def test_a_broken_source_is_reported_not_fatal(
        self, settings, profile, stub_pipeline, monkeypatch
    ):
        async def half_broken(sources, s=None):
            return [posting("Data Analyst", "AlphaCo")], [
                {"source": "Greenhouse board 'dead'", "error": "HTTP 404"}
            ]

        monkeypatch.setattr("jobsearch.pipeline.fetch_all", half_broken)
        stats, queued = await run(settings)
        assert stats.queued == 1
        assert stats.source_errors[0]["error"] == "HTTP 404"
        with session_scope(settings) as session:
            assert session.exec(select(RunLog)).one().source_errors
