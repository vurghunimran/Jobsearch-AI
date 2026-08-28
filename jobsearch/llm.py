"""Thin wrapper around the OpenAI SDK.

Two shapes are used across the app: free-form writing (cover letters,
statements of purpose) and schema-constrained extraction (fit scores,
screening answers). Everything else about model choice, effort and error
handling lives here, so the rest of the code stays declarative and no other
module imports an SDK.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import openai
from pydantic import BaseModel

from jobsearch.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: openai.OpenAI | None = None


class LLMError(RuntimeError):
    pass


def get_client(settings: Settings | None = None) -> openai.OpenAI:
    global _client
    if _client is None:
        settings = settings or get_settings()
        if not settings.openai_api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Add it to your .env — the agent needs it "
                "to score jobs and write your documents."
            )
        _client = openai.OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            max_retries=3,
            timeout=settings.llm_timeout,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests and after a settings change."""
    global _client
    _client = None


def _api_message(exc: openai.APIStatusError) -> str:
    """The human-readable reason the API gave, if it gave one."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(getattr(exc, "message", "") or "")


def _error_code(exc: openai.APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    return str(getattr(exc, "code", "") or "")


def _wrap(exc: Exception, what: str) -> LLMError:
    """Turn an SDK exception into something worth reading in a log at 08:00.

    A bare status code is useless to whoever has to fix it — the API almost
    always says exactly what is wrong, so pass that through.
    """
    if isinstance(exc, openai.AuthenticationError):
        return LLMError(f"{what}: OpenAI rejected the key in OPENAI_API_KEY.")

    if isinstance(exc, openai.RateLimitError):
        # OpenAI returns 429 both for genuine rate limiting and for an empty
        # balance. They need completely different fixes, so separate them.
        detail = _api_message(exc)
        if _error_code(exc) == "insufficient_quota" or "quota" in detail.lower():
            return LLMError(
                f"{what}: your OpenAI account is out of quota, so no jobs can be scored "
                "and no documents written. The API key itself is fine — add credit at "
                "https://platform.openai.com/settings/organization/billing."
            )
        return LLMError(f"{what}: rate limited by OpenAI — try a smaller run.")

    if isinstance(exc, openai.NotFoundError):
        return LLMError(
            f"{what}: OpenAI does not recognise the configured model. Run "
            "`jobsearch doctor` to list the models your account can actually use, "
            "then set MODEL in .env to one of them."
        )

    if isinstance(exc, openai.LengthFinishReasonError):
        return LLMError(
            f"{what}: the model hit its output limit before finishing. Raise "
            "max_tokens, or shorten the job description budget."
        )

    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return LLMError(f"{what}: OpenAI's content filter stopped this generation.")

    if isinstance(exc, openai.APIStatusError):
        detail = _api_message(exc)
        suffix = f" {detail}" if detail else ""
        return LLMError(f"{what}: OpenAI returned HTTP {exc.status_code}.{suffix}")

    if isinstance(exc, openai.APIConnectionError):
        return LLMError(f"{what}: could not reach OpenAI ({exc}).")

    return LLMError(f"{what}: {type(exc).__name__}: {exc}")


def _request_kwargs(settings: Settings, effort: str | None, max_tokens: int) -> dict[str, Any]:
    """Parameters common to both call shapes.

    `reasoning_effort` is only sent when set, because non-reasoning models
    reject it outright.
    """
    kwargs: dict[str, Any] = {"max_completion_tokens": max_tokens}
    if effort and effort != "none":
        kwargs["reasoning_effort"] = effort
    return kwargs


def generate_text(
    system: str,
    user: str,
    *,
    effort: str | None = None,
    max_tokens: int = 16000,
    settings: Settings | None = None,
    model: str | None = None,
) -> str:
    """Free-form generation. Streams, so long documents never hit a timeout."""
    settings = settings or get_settings()
    client = get_client(settings)
    try:
        stream = client.chat.completions.create(
            model=model or settings.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
            **_request_kwargs(settings, effort or settings.writing_effort, max_tokens),
        )
        parts: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content
            if piece:
                parts.append(piece)
    except Exception as exc:
        raise _wrap(exc, "Writing documents") from exc

    text = "".join(parts).strip()
    if not text:
        raise LLMError("Writing documents: the model returned nothing.")
    return text


def generate_structured(
    system: str,
    user: str,
    output_format: type[T],
    *,
    effort: str | None = None,
    max_tokens: int = 8000,
    settings: Settings | None = None,
    model: str | None = None,
) -> T:
    """Schema-constrained generation. The result is a validated model instance."""
    settings = settings or get_settings()
    client = get_client(settings)
    try:
        response = client.chat.completions.parse(
            model=model or settings.scoring_model or settings.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=output_format,
            **_request_kwargs(settings, effort or settings.scoring_effort, max_tokens),
        )
    except Exception as exc:
        raise _wrap(exc, f"Generating {output_format.__name__}") from exc

    message = response.choices[0].message
    if getattr(message, "refusal", None):
        raise LLMError(
            f"Generating {output_format.__name__}: the model declined — {message.refusal}"
        )
    parsed = message.parsed
    if parsed is None:
        raise LLMError(f"Generating {output_format.__name__}: no parsable result was returned.")
    return parsed


def list_models(settings: Settings | None = None) -> list[str]:
    """Model ids this account can use. Backs `jobsearch doctor`."""
    settings = settings or get_settings()
    try:
        return sorted(m.id for m in get_client(settings).models.list())
    except Exception as exc:
        raise _wrap(exc, "Listing models") from exc
