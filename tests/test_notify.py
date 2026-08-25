import pytest

from jobsearch.notify.email import EmailNotConfigured, _html, _load, _plain, _subject, send_digest


@pytest.fixture
def rows(settings, profile, seeded):
    return _load([seeded["application_id"]], settings)


class TestDigestContent:
    def test_subject_names_the_best_match(self, rows):
        assert _subject(rows) == "1 job to review: Data Analyst, Growth at ExampleCorp"

    def test_plain_text_carries_role_score_and_link(self, rows, settings):
        text = _plain(rows, {"fetched": 120, "new": 8, "filtered_out": 95, "scored": 25}, settings)
        assert "Data Analyst, Growth" in text
        assert "[88%]" in text
        assert f"{settings.resolved_base_url}/application/" in text
        assert "120 postings" in text

    def test_html_links_to_the_review_page(self, rows, settings):
        html = _html(rows, {"fetched": 1}, settings)
        assert f"{settings.resolved_base_url}/application/" in html
        assert "Review &amp; approve" in html
        # No external assets: the email must render offline and leak no pixels.
        assert "http://" not in html.replace(settings.resolved_base_url, "")

    def test_html_escapes_hostile_content(self, settings, profile, seeded):
        from jobsearch.db import session_scope
        from jobsearch.models import Job

        with session_scope(settings) as session:
            job = session.get(Job, seeded["job_id"])
            job.company = '<script>alert("x")</script>'
            session.add(job)
        html = _html(_load([seeded["application_id"]], settings), {}, settings)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestTokenisedLinks:
    """A digest opened on a phone must sign the reader in."""

    def test_links_carry_the_token_when_one_is_set(self, rows, settings, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_token", "s3cret")
        html = _html(rows, {}, settings)
        text = _plain(rows, {}, settings)
        assert "?token=s3cret" in html
        assert "?token=s3cret" in text

    def test_no_token_means_no_query_string(self, rows, settings):
        assert "?token=" not in _html(rows, {}, settings)

    def test_a_token_with_url_characters_is_escaped(self, rows, settings, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_token", "a b&c")
        assert "?token=a%20b%26c" in _html(rows, {}, settings)


class TestSending:
    def test_refuses_when_smtp_is_not_configured(self, settings, seeded):
        with pytest.raises(EmailNotConfigured, match="SMTP_HOST"):
            send_digest([seeded["application_id"]], {}, settings)

    def test_no_applications_means_no_email(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "me@example.com")
        monkeypatch.setattr(settings, "digest_to", "me@example.com")

        def explode(*args, **kwargs):
            raise AssertionError("should not have tried to send")

        monkeypatch.setattr("jobsearch.notify.email._send", explode)
        send_digest([], {}, settings)

    def test_builds_a_multipart_message(self, settings, seeded, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from", "me@example.com")
        monkeypatch.setattr(settings, "digest_to", "you@example.com")
        captured = {}
        monkeypatch.setattr(
            "jobsearch.notify.email._send", lambda message, s: captured.update(message=message)
        )
        send_digest([seeded["application_id"]], {"fetched": 3}, settings)

        message = captured["message"]
        assert message["To"] == "you@example.com"
        assert "Data Analyst, Growth" in message["Subject"]
        assert {part.get_content_type() for part in message.walk()} >= {
            "text/plain",
            "text/html",
        }
