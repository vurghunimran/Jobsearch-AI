import httpx
import pytest
import respx

from jobsearch.apply.base import ApplicationPacket
from jobsearch.apply.engine import blockers, build_request, submit
from jobsearch.apply.manual import build_packet
from jobsearch.apply.questions import normalise_greenhouse
from jobsearch.apply.recipes import GREENHOUSE, LEVER, recipe_for
from jobsearch.apply.service import load_packet, submit_application
from jobsearch.config import SubmitMode
from jobsearch.db import session_scope
from jobsearch.models import ATS, Application, ApplicationStatus, Job


@pytest.fixture
def packet(settings, profile, seeded) -> ApplicationPacket:
    with session_scope(settings) as session:
        application = session.get(Application, seeded["application_id"])
        return load_packet(session, application, profile, settings)


class TestBuildRequest:
    def test_maps_canonical_fields_to_greenhouse_names(self, packet):
        built = build_request(GREENHOUSE, packet)
        assert built.url == "https://boards.greenhouse.io/embed/job_app?token=4567"
        assert built.data["first_name"] == "Jane"
        assert built.data["last_name"] == "Rivera"
        assert built.data["email"] == "jane@example.com"
        assert "growth reporting" in built.data["cover_letter_text"]

    def test_screening_answers_become_form_fields(self, packet):
        built = build_request(GREENHOUSE, packet)
        assert built.data["question_9001"].startswith("Growth analytics")
        assert built.data["question_9002"] == "EUR 55,000"

    def test_lever_uses_its_own_field_names(self, packet):
        packet.job.ats = ATS.lever
        packet.job.ats_meta = {"company": "examplecorp", "posting_id": "abc-123"}
        built = build_request(LEVER, packet)
        assert built.url == "https://jobs.lever.co/examplecorp/abc-123/apply"
        assert built.data["name"] == "Jane Rivera"
        assert built.data["urls[LinkedIn]"].endswith("janerivera")
        assert built.data["cards[question_9001]"].startswith("Growth analytics")

    def test_attaches_the_cv(self, packet):
        built = build_request(GREENHOUSE, packet)
        assert built.resume_filename == "cv.md"
        assert built.resume_bytes
        assert built.httpx_files("resume")["resume"][0] == "cv.md"

    def test_preview_truncates_text_and_omits_file_bytes(self, packet):
        packet.documents[0].content = "x" * 5000
        preview = build_request(GREENHOUSE, packet).preview()
        assert len(preview["fields"]["cover_letter_text"]) < 500
        assert preview["file"]["bytes"] > 0
        assert "resume_bytes" not in preview


class TestBlockers:
    def test_flags_unconfirmed_answers(self, packet):
        reasons = blockers(packet)
        assert any("flagged as guesses" in r for r in reasons)

    def test_clean_packet_has_none(self, packet):
        for answer in packet.answers:
            answer["needs_human"] = False
        assert blockers(packet) == []

    def test_flags_a_missing_cv(self, packet):
        for answer in packet.answers:
            answer["needs_human"] = False
        packet.resume_path = None
        assert any("No CV file" in r for r in blockers(packet))

    def test_flags_an_incomplete_profile(self, packet):
        packet.profile.identity.email = ""
        assert any("missing" in r for r in blockers(packet))


class TestSubmit:
    async def test_dry_run_sends_nothing_but_shows_the_request(self, packet):
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.dry_run)
        assert result.status == "dry_run"
        assert result.request_preview["fields"]["email"] == "jane@example.com"
        # A dry run still reports what would have stopped a live send.
        assert result.request_preview["blockers"]

    async def test_live_refuses_an_unverified_recipe(self, packet):
        for answer in packet.answers:
            answer["needs_human"] = False
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.live)
        assert result.status == "needs_manual"
        assert "not been verified" in result.detail

    async def test_live_refuses_while_answers_are_unconfirmed(self, packet):
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.live)
        assert result.status == "needs_manual"
        assert "flagged as guesses" in result.detail

    @respx.mock
    async def test_verified_recipe_posts_and_reports_success(self, packet, monkeypatch):
        monkeypatch.setattr(GREENHOUSE, "verified", True)
        for answer in packet.answers:
            answer["needs_human"] = False
        route = respx.post("https://boards.greenhouse.io/embed/job_app").mock(
            return_value=httpx.Response(200, text="Thanks for applying!")
        )
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.live)
        assert result.status == "submitted"
        assert route.called
        assert b'name="resume"' in route.calls[0].request.content

    @respx.mock
    async def test_http_error_is_a_failure_not_a_silent_success(self, packet, monkeypatch):
        monkeypatch.setattr(GREENHOUSE, "verified", True)
        for answer in packet.answers:
            answer["needs_human"] = False
        respx.post("https://boards.greenhouse.io/embed/job_app").mock(
            return_value=httpx.Response(422, text="unprocessable")
        )
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.live)
        assert result.status == "failed" and "422" in result.detail

    @respx.mock
    async def test_a_200_that_says_error_is_treated_as_failure(self, packet, monkeypatch):
        monkeypatch.setattr(GREENHOUSE, "verified", True)
        for answer in packet.answers:
            answer["needs_human"] = False
        respx.post("https://boards.greenhouse.io/embed/job_app").mock(
            return_value=httpx.Response(200, text="There was an error with your application")
        )
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.live)
        assert result.status == "failed"

    async def test_missing_ats_metadata_becomes_manual(self, packet):
        packet.job.ats_meta = {}
        async with httpx.AsyncClient() as client:
            result = await submit(GREENHOUSE, packet, client, SubmitMode.dry_run)
        assert result.status == "needs_manual"


class TestManualPacket:
    def test_writes_every_document_and_the_instructions(self, packet, settings):
        result = build_packet(packet, "No automated route.", settings)
        assert result.status == "needs_manual"
        directory = settings.output_dir / result.request_preview["packet_dir"].split("/")[-1]
        names = {p.name for p in directory.iterdir()}
        assert {"APPLY.txt", "cover_letter.md", "cover_letter.pdf", "cover_letter.docx"} <= names
        assert "cv.md" in names, "the CV is copied in so the folder is self-contained"

        instructions = (directory / "APPLY.txt").read_text()
        assert "Jane Rivera" in instructions
        assert "jane@example.com" in instructions
        assert "boards.greenhouse.io/examplecorp/jobs/4567" in instructions
        assert "CHECK THIS" in instructions, "guessed answers must be called out"


class TestService:
    async def test_ashby_falls_through_to_a_manual_packet(self, settings, profile, seeded):
        with session_scope(settings) as session:
            job = session.get(Job, seeded["job_id"])
            job.ats = ATS.ashby
            session.add(job)

        with session_scope(settings) as session:
            application = session.get(Application, seeded["application_id"])
            result = await submit_application(session, application, profile, settings)

        assert result.status == "needs_manual"
        assert "Ashby" in result.detail
        with session_scope(settings) as session:
            application = session.get(Application, seeded["application_id"])
            assert application.status is ApplicationStatus.needs_manual
            assert application.packet_dir
            assert len(application.submit_log) == 1

    async def test_the_outcome_is_recorded_on_the_application(self, settings, profile, seeded):
        with session_scope(settings) as session:
            application = session.get(Application, seeded["application_id"])
            await submit_application(session, application, profile, settings, SubmitMode.dry_run)
        with session_scope(settings) as session:
            application = session.get(Application, seeded["application_id"])
            assert application.status is ApplicationStatus.dry_run
            assert application.submit_log[0]["mode"] == "dry_run"


class TestQuestionNormalisation:
    def test_extracts_custom_questions_and_options(self):
        questions = normalise_greenhouse(
            [
                {
                    "label": "Resume",
                    "required": True,
                    "fields": [{"name": "resume", "type": "input_file", "values": []}],
                },
                {
                    "label": "Why us?",
                    "required": True,
                    "fields": [{"name": "question_1", "type": "textarea", "values": []}],
                },
                {
                    "label": "Authorized?",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_2",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
            ]
        )
        assert [q["field_id"] for q in questions] == ["question_1", "question_2"]
        assert questions[1]["options"] == ["Yes", "No"]

    def test_skips_builtin_and_file_fields(self):
        assert (
            normalise_greenhouse(
                [
                    {
                        "label": "First Name",
                        "fields": [{"name": "first_name", "type": "input_text"}],
                    },
                    {
                        "label": "Portfolio",
                        "fields": [{"name": "question_9", "type": "input_file"}],
                    },
                ]
            )
            == []
        )

    def test_tolerates_empty_input(self):
        assert normalise_greenhouse([]) == []


def test_ashby_and_workable_have_no_recipe_by_design():
    assert recipe_for(ATS.ashby) is None
    assert recipe_for(ATS.workable) is None
    assert recipe_for(ATS.greenhouse) is not None
