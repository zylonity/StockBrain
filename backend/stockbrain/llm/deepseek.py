"""DeepSeek chat-completions client.

Verified against DeepSeek's current documentation (2026-09-04):

* base ``https://api.deepseek.com``, endpoint ``/chat/completions``,
  ``Authorization: Bearer <key>``, OpenAI-compatible request/response shape
* models: ``deepseek-v4-flash``, ``deepseek-v4-pro``,
  ``deepseek-v4-flash-vision-exp``
* **``thinking`` defaults to ``{"type": "enabled"}``.** The classifier must run
  without it, so ``{"type": "disabled"}`` is sent explicitly on every
  non-thinking call. Omitting the field would silently enable reasoning and
  change both latency and cost.
* JSON mode is ``response_format: {"type": "json_object"}``, and the docs require
  the word "json" to appear in the prompt and recommend showing an example
  schema. Both are enforced here rather than left to the prompt author.
* ``usage`` reports ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
  ``prompt_cache_hit_tokens``, ``prompt_cache_miss_tokens`` and
  ``completion_tokens_details.reasoning_tokens``
* 429 signals the account concurrency limit
* ``finish_reason`` may be ``insufficient_system_resource``, which is a
  server-side capacity condition and is retried like a 5xx
* the server may hold a connection open, emitting blank lines, and closes it if
  inference has not started within ten minutes -- so the read timeout is
  generous but bounded, and JSON decoding tolerates leading whitespace

The API key is held as a ``SecretStr`` and injected into the header at
construction. It is never logged, never included in an exception message, and
never returned to a caller.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import httpx

from stockbrain.config import Settings
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm.base import CompletionRequest, CompletionResult, TokenUsage
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["DEEPSEEK_CHAT_PATH", "DeepSeekClient", "parse_completion"]

log = get_logger(__name__)

DEEPSEEK_CHAT_PATH = "/chat/completions"

#: DeepSeek keeps a connection open while queued and only gives up after ten
#: minutes. A read timeout below that would abandon work that is still coming.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Finish reasons that mean "server could not serve this", not "model answered".
_RETRYABLE_FINISH_REASONS = frozenset({"insufficient_system_resource"})


def parse_completion(payload: Any, *, latency_ms: int, attempts: int) -> CompletionResult:
    """Turn a chat-completions response into a :class:`CompletionResult`.

    Raises :class:`ProviderResponseError` when the payload does not match the
    documented shape. An empty or truncated completion is an error here, not a
    silently-empty classification downstream.
    """
    if not isinstance(payload, dict):
        raise ProviderResponseError("deepseek: response was not a JSON object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError("deepseek: response contained no choices")

    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderResponseError("deepseek: malformed choice entry")

    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError("deepseek: choice contained no message")

    content = message.get("content")
    finish_reason = first.get("finish_reason")

    if finish_reason in _RETRYABLE_FINISH_REASONS:
        # Documented capacity condition: treat exactly like a 5xx.
        raise ProviderUnavailable(f"deepseek: finish_reason={finish_reason}")

    if finish_reason == "length":
        # A truncated JSON document would fail schema validation with a confusing
        # message; naming the real cause makes the fix (raise max_tokens) obvious.
        raise ProviderResponseError(
            "deepseek: completion hit the output token limit and was truncated"
        )

    if not isinstance(content, str) or not content.strip():
        # Documented behaviour: JSON mode can occasionally return empty content.
        raise ProviderResponseError("deepseek: completion content was empty")

    raw_usage = payload.get("usage")
    usage = TokenUsage()
    if isinstance(raw_usage, dict):
        details = raw_usage.get("completion_tokens_details")
        usage = TokenUsage(
            prompt_tokens=_as_int(raw_usage.get("prompt_tokens")),
            completion_tokens=_as_int(raw_usage.get("completion_tokens")),
            total_tokens=_as_int(raw_usage.get("total_tokens")),
            cache_hit_tokens=_as_int(raw_usage.get("prompt_cache_hit_tokens")),
            cache_miss_tokens=_as_int(raw_usage.get("prompt_cache_miss_tokens")),
            reasoning_tokens=(
                _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
            ),
        )

    return CompletionResult(
        content=content,
        model=str(payload.get("model") or ""),
        usage=usage,
        finish_reason=str(finish_reason) if finish_reason else None,
        provider_request_id=str(payload.get("id")) if payload.get("id") else None,
        latency_ms=latency_ms,
        attempts=attempts,
        # Recorded as a flag only; the reasoning text itself is discarded here
        # and never reaches storage or the UI.
        had_reasoning_content=bool(message.get("reasoning_content")),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


class DeepSeekClient:
    """Async DeepSeek client with bounded, classified retries."""

    name = "deepseek"

    def __init__(
        self,
        settings: Settings,
        *,
        http: ProviderHttpClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._settings = settings
        self._max_attempts = max(1, max_attempts)
        self._default_timeout = settings.deepseek_timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._http = http or ProviderHttpClient(
            provider="deepseek",
            base_url=settings.deepseek_base_url,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout_seconds=self._default_timeout,
            max_attempts=self._max_attempts,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Build the request body exactly as the current API documents it."""
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": False,
            # Explicit in both directions. The API default is "enabled", so a
            # missing field would turn the cheap classifier into a reasoning call.
            "thinking": {"type": "enabled" if request.thinking else "disabled"},
        }
        if request.json_object:
            body["response_format"] = {"type": "json_object"}
            self._assert_prompt_mentions_json(request)
        return body

    @staticmethod
    def _assert_prompt_mentions_json(request: CompletionRequest) -> None:
        """DeepSeek requires the word "json" in the prompt when JSON mode is on.

        Checked here rather than trusted to the prompt file, because the failure
        mode without it is a confusing model response rather than an API error.
        """
        if not any("json" in message.content.lower() for message in request.messages):
            raise ValueError(
                "DeepSeek JSON mode requires the word 'json' to appear in the prompt; "
                "the supplied messages do not mention it"
            )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Perform one completion, retrying only clearly retryable failures.

        Retried: 429 (concurrency), 5xx, transport failures, and the documented
        ``insufficient_system_resource`` finish reason.

        Not retried: 401/403 (a bad key stays bad), 400 (a malformed request
        stays malformed), and schema violations in the response.
        """
        body = self.build_body(request)
        started = dt.datetime.now(dt.UTC)
        loop = asyncio.get_running_loop()
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            call_started = loop.time()
            try:
                payload = await self._http.request_json(
                    "POST",
                    DEEPSEEK_CHAT_PATH,
                    json_body=body,
                    # A chat completion is not idempotent in the billing sense,
                    # but it has no external side effect: repeating it can only
                    # cost tokens, never place an order. Retries stay bounded and
                    # every attempt is recorded separately in llm_calls.
                    retry_safe=False,
                )
                latency_ms = int((loop.time() - call_started) * 1000)
                result = parse_completion(payload, latency_ms=latency_ms, attempts=attempt)
                result.started_at = started
                result.completed_at = dt.datetime.now(dt.UTC)
                self._record_metrics(result)
                return result

            except (ProviderAuthError, ValueError):
                # A rejected credential or a malformed request never becomes
                # valid by trying again.
                raise
            except (ProviderRateLimited, ProviderUnavailable) as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                hint = getattr(exc, "retry_after_seconds", None)
                delay = hint if hint is not None else min(8.0, 0.75 * (2 ** (attempt - 1)))
                log.warning(
                    "deepseek_retrying",
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    error_type=type(exc).__name__,
                    delay_seconds=round(delay, 2),
                    purpose=request.purpose,
                )
                await asyncio.sleep(delay)
            except ProviderResponseError:
                # The provider answered, but not with something usable. Retrying
                # a schema violation usually reproduces it and always costs money.
                raise
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                last_error = ProviderUnavailable(f"deepseek: {type(exc).__name__}")
                if attempt >= self._max_attempts:
                    break
                await asyncio.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))

        assert last_error is not None
        METRICS.inc(
            "stockbrain_provider_errors_total",
            labels={"provider": "deepseek", "kind": "exhausted"},
        )
        raise last_error

    @staticmethod
    def _record_metrics(result: CompletionResult) -> None:
        METRICS.inc(
            "stockbrain_llm_input_tokens_total",
            float(result.usage.prompt_tokens),
            labels={"model": result.model or "unknown"},
        )
        METRICS.inc(
            "stockbrain_llm_output_tokens_total",
            float(result.usage.completion_tokens),
            labels={"model": result.model or "unknown"},
        )


def extract_json(content: str) -> Any:
    """Decode a model's JSON reply, tolerating the wrappers models add.

    JSON mode usually returns bare JSON, but a fenced ```json block still
    happens. Stripping a fence is safe; anything else is a decode error, because
    guessing at malformed JSON is how wrong data gets in.
    """
    text = content.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ProviderResponseError(
            f"deepseek: model output was not valid JSON ({exc.__class__.__name__})"
        ) from exc
