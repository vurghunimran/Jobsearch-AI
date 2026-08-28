"""Error reporting.

These run offline: they construct SDK exceptions directly rather than calling
the API. What matters is the message a user reads in a log at 08:00, not the
HTTP round trip.
"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from jobsearch.llm import LLMError, _wrap, get_client, reset_client


def status_error(cls, status: int, message: str, code: str = "") -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error: dict[str, str] = {"message": message, "type": "invalid_request_error"}
    if code:
        error["code"] = code
    body = {"error": error}
    return cls(message, response=httpx.Response(status, json=body, request=request), body=body)


class TestQuotaVersusRateLimit:
    """OpenAI returns 429 for both. They need completely different fixes."""

    def test_an_empty_balance_says_so_and_says_where_to_fix_it(self):
        exc = status_error(
            openai.RateLimitError,
            429,
            "You exceeded your current quota, please check your plan and billing details.",
            code="insufficient_quota",
        )
        message = str(_wrap(exc, "Scoring jobs"))
        assert "out of quota" in message
        assert "platform.openai.com/settings/organization/billing" in message
        # The key is valid here; blaming it sends people hunting in the wrong place.
        assert "rejected" not in message

    def test_genuine_rate_limiting_suggests_a_smaller_run(self):
        exc = status_error(
            openai.RateLimitError,
            429,
            "Rate limit reached for requests",
            code="rate_limit_exceeded",
        )
        message = str(_wrap(exc, "Scoring jobs"))
        assert "smaller run" in message
        assert "out of quota" not in message

    def test_quota_is_detected_from_the_message_when_no_code_is_given(self):
        exc = status_error(openai.RateLimitError, 429, "You exceeded your current quota.")
        assert "out of quota" in str(_wrap(exc, "Scoring jobs"))


class TestOtherFailures:
    def test_a_bad_key_is_named_as_a_key_problem(self):
        exc = status_error(openai.AuthenticationError, 401, "Incorrect API key provided")
        assert "OPENAI_API_KEY" in str(_wrap(exc, "Scoring jobs"))

    def test_an_unknown_model_points_at_doctor(self):
        exc = status_error(openai.NotFoundError, 404, "The model 'gpt-nope' does not exist")
        message = str(_wrap(exc, "Writing documents"))
        assert "jobsearch doctor" in message
        assert "MODEL" in message

    def test_other_status_errors_pass_the_api_explanation_through(self):
        exc = status_error(openai.BadRequestError, 400, "max_completion_tokens must be > 0")
        message = str(_wrap(exc, "Writing documents"))
        assert "HTTP 400" in message
        assert "max_completion_tokens must be > 0" in message

    def test_a_status_error_without_a_body_still_reads_sensibly(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        exc = openai.InternalServerError(
            "boom", response=httpx.Response(503, request=request), body=None
        )
        assert "HTTP 503" in str(_wrap(exc, "Scoring jobs"))

    def test_hitting_the_output_limit_says_which_knob_to_turn(self):
        # The SDK reads completion.usage when building its message.
        completion = SimpleNamespace(usage=None)
        exc = openai.LengthFinishReasonError(completion=completion)
        assert "max_tokens" in str(_wrap(exc, "Writing documents"))

    def test_a_content_filter_stop_is_named_as_such(self):
        exc = openai.ContentFilterFinishReasonError()
        assert "content filter" in str(_wrap(exc, "Writing documents"))

    def test_the_operation_is_always_named(self):
        assert str(_wrap(RuntimeError("odd"), "Writing documents")).startswith("Writing documents")


class TestClientSetup:
    def test_a_missing_key_explains_what_to_do(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "")
        reset_client()
        with pytest.raises(LLMError, match="OPENAI_API_KEY is not set"):
            get_client(settings)
        reset_client()

    def test_the_client_is_built_from_settings(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "sk-test")
        reset_client()
        client = get_client(settings)
        assert client.api_key == "sk-test"
        reset_client()


class TestEffortConfiguration:
    def test_reasoning_effort_is_omitted_when_unset(self, settings):
        from jobsearch.llm import _request_kwargs

        # Non-reasoning models reject the parameter outright, so it must not
        # be sent unless asked for.
        assert "reasoning_effort" not in _request_kwargs(settings, None, 100)
        assert "reasoning_effort" not in _request_kwargs(settings, "none", 100)

    def test_effort_is_passed_through_when_set(self, settings):
        from jobsearch.llm import _request_kwargs

        kwargs = _request_kwargs(settings, "low", 4000)
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["max_completion_tokens"] == 4000
