"""Error reporting.

These run offline: they construct SDK exceptions directly rather than calling
the API. The point is the message a user reads in a log, not the HTTP round trip.
"""

import anthropic
import httpx
import pytest

from jobsearch.llm import LLMError, _wrap, get_client, reset_client


def status_error(status: int, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(status, json=body, request=request)
    return anthropic.APIStatusError(message, response=response, body=body)


class TestErrorMessages:
    def test_an_empty_balance_says_so_and_says_where_to_fix_it(self):
        exc = status_error(
            400, "Your credit balance is too low to access the Anthropic API."
        )
        message = str(_wrap(exc, "Scoring jobs"))
        assert "out of credit" in message
        assert "console.anthropic.com/settings/billing" in message
        # The key is valid in this case; saying otherwise sends people hunting
        # in the wrong place.
        assert "rejected" not in message

    def test_other_status_errors_pass_the_api_explanation_through(self):
        exc = status_error(400, "max_tokens: must be greater than 0")
        message = str(_wrap(exc, "Writing documents"))
        assert "HTTP 400" in message
        assert "max_tokens: must be greater than 0" in message

    def test_a_status_error_without_a_body_still_reads_sensibly(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.APIStatusError(
            "boom", response=httpx.Response(503, request=request), body=None
        )
        assert "HTTP 503" in str(_wrap(exc, "Scoring jobs"))

    def test_a_bad_key_is_named_as_a_key_problem(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=request), body=None
        )
        assert "ANTHROPIC_API_KEY" in str(_wrap(exc, "Scoring jobs"))

    def test_rate_limiting_suggests_a_smaller_run(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        )
        assert "smaller run" in str(_wrap(exc, "Scoring jobs"))

    def test_the_operation_is_always_named(self):
        assert str(_wrap(RuntimeError("odd"), "Writing documents")).startswith("Writing documents")


class TestClientSetup:
    def test_a_missing_key_explains_what_to_do(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "anthropic_api_key", "")
        reset_client()
        with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
            get_client(settings)
        reset_client()
