import pytest
from fastapi.testclient import TestClient

from jobsearch.db import session_scope
from jobsearch.models import Application, ApplicationStatus, Document, DocumentKind
from jobsearch.web.app import create_app


@pytest.fixture
def client(settings, profile, seeded):
    with TestClient(create_app(settings)) as test_client:
        test_client.seeded = seeded
        yield test_client


class TestPages:
    def test_every_page_renders(self, client):
        app_id = client.seeded["application_id"]
        for path in ("/healthz", "/", f"/application/{app_id}", "/applications", "/jobs"):
            assert client.get(path).status_code == 200, path

    def test_the_queue_shows_the_role_and_its_score(self, client):
        body = client.get("/").text
        assert "Data Analyst, Growth" in body
        assert "ExampleCorp" in body
        assert "88" in body

    def test_the_detail_page_shows_documents_answers_and_reasoning(self, client):
        body = client.get(f"/application/{client.seeded['application_id']}").text
        assert "Cover letter" in body
        assert "growth reporting at Bolt" in body
        assert "Expected salary?" in body
        assert "Four years of exactly this work." in body
        assert "No experimentation platform work" in body

    def test_a_guessed_answer_is_visibly_flagged(self, client):
        body = client.get(f"/application/{client.seeded['application_id']}").text
        assert "needs-check" in body
        assert "Guessed" in body

    def test_unknown_application_is_a_404(self, client):
        assert client.get("/application/9999").status_code == 404

    def test_the_jobs_page_explains_rejections(self, client, settings):
        from jobsearch.models import Job, JobStatus

        with session_scope(settings) as session:
            session.add(
                Job(
                    fingerprint="fp-rejected",
                    source="test",
                    title="Data Engineer",
                    company="BetaCo",
                    status=JobStatus.filtered_out,
                    filter_reason="title does not match any of your target titles",
                )
            )
        body = client.get("/jobs?show=filtered").text
        assert "title does not match any of your target titles" in body


class TestActions:
    def test_approving_marks_the_application(self, client, settings):
        app_id = client.seeded["application_id"]
        assert (
            client.post(f"/application/{app_id}/approve", follow_redirects=False).status_code == 303
        )
        with session_scope(settings) as session:
            application = session.get(Application, app_id)
            assert application.status is ApplicationStatus.approved
            assert application.reviewed_at is not None

    def test_skipping_removes_it_from_the_queue(self, client, settings):
        app_id = client.seeded["application_id"]
        client.post(f"/application/{app_id}/reject", follow_redirects=False)
        with session_scope(settings) as session:
            assert session.get(Application, app_id).status is ApplicationStatus.rejected
        assert "Data Analyst, Growth" not in client.get("/").text

    def test_editing_a_document_persists_and_is_marked(self, client, settings):
        app_id = client.seeded["application_id"]
        with session_scope(settings) as session:
            doc_id = (
                session.exec(
                    __import__("sqlmodel").select(Document).where(Document.application_id == app_id)
                )
                .first()
                .id
            )

        client.post(
            f"/application/{app_id}/document/{doc_id}",
            data={"content": "My own words."},
            follow_redirects=False,
        )
        with session_scope(settings) as session:
            document = session.get(Document, doc_id)
            assert document.content == "My own words."
            assert document.edited_by_user is True

    def test_editing_a_document_from_another_application_is_rejected(self, client, settings):
        # A document id alone must not be enough: it has to belong to the
        # application in the URL.
        with session_scope(settings) as session:
            other = Application(job_id=client.seeded["job_id"])
            session.add(other)
            session.commit()
            foreign = Document(
                application_id=other.id, kind=DocumentKind.cover_letter, content="theirs"
            )
            session.add(foreign)
            session.commit()
            foreign_id = foreign.id

        response = client.post(
            f"/application/{client.seeded['application_id']}/document/{foreign_id}",
            data={"content": "hijacked"},
            follow_redirects=False,
        )
        assert response.status_code == 404
        with session_scope(settings) as session:
            assert session.get(Document, foreign_id).content == "theirs"

    def test_saving_answers_persists_and_clears_the_guess_flag(self, client, settings):
        app_id = client.seeded["application_id"]
        client.post(
            f"/application/{app_id}/answers",
            data={"answer_0": "Because growth.", "answer_1": "EUR 60,000"},
            follow_redirects=False,
        )
        with session_scope(settings) as session:
            answers = session.get(Application, app_id).answers
        assert answers[1]["answer"] == "EUR 60,000"
        # Editing an answer is the human confirmation the writer asked for.
        assert [a["needs_human"] for a in answers] == [False, False]


class TestAuth:
    """The token gate matters only when the dashboard is reachable remotely."""

    @pytest.fixture
    def protected(self, settings, profile, seeded, monkeypatch):
        from jobsearch.config import get_settings

        monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
        get_settings.cache_clear()
        resolved = get_settings()
        with TestClient(create_app(resolved)) as test_client:
            test_client.seeded = seeded
            test_client.settings = resolved
            yield test_client

    def test_blocks_without_a_token(self, protected):
        assert protected.get("/").status_code == 401

    def test_admits_with_the_token_in_the_url(self, protected):
        assert protected.get("/?token=s3cret").status_code == 200

    def test_rejects_a_wrong_token(self, protected):
        assert protected.get("/?token=nope").status_code == 401

    def test_a_url_token_is_exchanged_for_a_cookie(self, protected):
        # Arriving from the digest email must sign you in, so that every link
        # from there on works without the token in the query string.
        protected.get("/?token=s3cret")
        assert protected.cookies.get("jobsearch_token") == "s3cret"
        assert protected.get("/applications").status_code == 200
        detail = protected.get(f"/application/{protected.seeded['application_id']}")
        assert detail.status_code == 200

    def test_post_actions_are_protected_too(self, settings, profile, seeded, monkeypatch):
        from jobsearch.config import get_settings

        monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
        get_settings.cache_clear()
        with TestClient(create_app(get_settings())) as client:
            response = client.post(
                f"/application/{seeded['application_id']}/approve", follow_redirects=False
            )
            assert response.status_code == 401
        with session_scope(settings) as session:
            assert session.get(Application, seeded["application_id"]).status is (
                ApplicationStatus.pending_review
            ), "an unauthenticated POST must not change anything"

    def test_healthz_and_static_stay_open(self, protected):
        # The container health check has no token, and the browser fetches CSS
        # without the query string.
        assert protected.get("/healthz").status_code == 200
        assert protected.get("/static/app.css").status_code == 200


class TestSchedulerVisibility:
    """A headless box must be able to show whether tomorrow's search is armed."""

    def test_footer_shows_when_the_next_search_runs(self, client):
        body = client.get("/").text
        assert "Next search:" in body
        assert "No scheduler running" not in body

    def test_footer_warns_loudly_when_no_scheduler_is_running(
        self, settings, profile, seeded, monkeypatch
    ):
        # A silently dead scheduler means no job postings for a week and no
        # sign anything is wrong, so the dashboard has to say so.
        monkeypatch.setattr(
            "jobsearch.scheduler.start_scheduler",
            lambda s=None: (_ for _ in ()).throw(RuntimeError("bad cron")),
        )
        with TestClient(create_app(settings)) as client:
            body = client.get("/").text
        assert "No scheduler running" in body
        assert "DIGEST_CRON" in body

    def test_the_scheduled_job_exists_with_a_next_run_time(self, settings, profile, seeded):
        app = create_app(settings)
        with TestClient(app):
            job = app.state.scheduler.get_job("daily-discovery")
            assert job is not None
            assert job.next_run_time is not None
