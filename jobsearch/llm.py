"""Thin wrapper around the Anthropic SDK.

Two shapes are used across the app: free-form writing (cover letters,
statements of purpose) and schema-constrained extraction (fit scores,
screening answers). Everything else about model choice, effort and error
handling lives here so the rest of the code stays declarative.
"""

from __future__ import annotations

import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from jobsearch.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class LLMError(RuntimeError):
    pass


def get_client(settings: Settings | None = None) -> anthropic.Anthropic:
    global _client
    if _client is None:
        settings = settings or get_settings()
        if not settings.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env — the agent needs it "
                "to score jobs and write your documents."
            )
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests."""
    global _client
    _client = None


def _api_message(exc: anthropic.APIStatusError) -> str:
    """The human-readable reason the API gave, if it gave one."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return ""


def _wrap(exc: Exception, what: str) -> LLMError:
    """Turn an SDK exception into something worth reading in a log at 08:00.

    A bare status code is useless to whoever has to fix it — the API almost
    always says exactly what is wrong, so pass that through.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError(f"{what}: rate limited by the Claude API — try a smaller run.")
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError(f"{what}: Claude API rejected the key in ANTHROPIC_API_KEY.")
    if isinstance(exc, anthropic.APIStatusError):
        detail = _api_message(exc)
        # By far the most common 400 in practice, and entirely actionable.
        if "credit balance" in detail.lower():
            return LLMError(
                f"{what}: your Anthropic account is out of credit, so no jobs can be "
                "scored and no documents written. The API key itself is fine — add "
                "credit at https://console.anthropic.com/settings/billing."
            )
        suffix = f" {detail}" if detail else ""
        return LLMError(f"{what}: Claude API returned HTTP {exc.status_code}.{suffix}")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError(f"{what}: could not reach the Claude API ({exc}).")
    return LLMError(f"{what}: {type(exc).__name__}: {exc}")


def generate_text(
    system: str,
    user: str,
    *,
    effort: str | None = None,
    max_tokens: int = 16000,
    settings: Settings | None = None,
) -> str:
    """Free-form generation. Streams, so long documents never hit a timeout."""
    settings = settings or get_settings()
    client = get_client(settings)
    try:
        with client.messages.stream(
            model=settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort or settings.writing_effort},
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:
        raise _wrap(exc, "Writing documents") from exc

    if message.stop_reason == "refusal":
        raise LLMError("Claude declined to write this document.")
    return "".join(block.text for block in message.content if block.type == "text").strip()


def generate_structured(
    system: str,
    user: str,
    output_format: type[T],
    *,
    effort: str | None = None,
    max_tokens: int = 8000,
    settings: Settings | None = None,
) -> T:
    """Schema-constrained generation. The result is a validated model instance."""
    settings = settings or get_settings()
    client = get_client(settings)
    try:
        response = client.messages.parse(
            model=settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort or settings.scoring_effort},
            output_format=output_format,
        )
    except Exception as exc:
        raise _wrap(exc, f"Generating {output_format.__name__}") from exc

    if response.stop_reason == "refusal":
        raise LLMError(f"Claude declined to produce {output_format.__name__}.")
    parsed = response.parsed_output
    if parsed is None:
        raise LLMError(f"Claude returned no parsable {output_format.__name__}.")
    return parsed
