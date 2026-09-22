"""Generic OpenAI-compatible chat-completions client.

One request builder and one response parser, both driven entirely by a
:class:`~stockbrain.llm.profiles.ProviderProfile`. Every provider difference this
codebase depends on is a field on the profile, so this module contains no
provider names and no ``if provider ==`` branches.

The retry policy is the one StockBrain already had and is deliberately narrow:

* retried -- 429, 5xx, transport failures, and the profile's own
  "server could not serve this" finish reasons
* not retried -- 401/403 (a bad key stays bad), 400 (a malformed request stays
  malformed), and schema violations in the response (retrying reproduces them
  and always costs money)

Every attempt is recorded separately by the telemetry layer, so a retry that
returns a different answer is never ambiguous after the fact.

The API key is held as a ``SecretStr`` by configuration and injected into the
header at construction. It is never logged, never placed in an exception
message, and never returned to a caller.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from typing import Any

import httpx

from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm.base import CompletionRequest, CompletionResult, TokenUsage
from stockbrain.llm.profiles import ProviderProfile
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "OpenAICompatibleClient",
    "build_body",
    "extract_json",
    "parse_completion",
    "strict_json_schema",
]

log = get_logger(__name__)

#: A queued request may hold a connection open for minutes before inference
#: starts. A read timeout below that abandons work that is still coming.
DEFAULT_TIMEOUT_SECONDS = 120.0


def build_body(request: CompletionRequest, profile: ProviderProfile) -> dict[str, Any]:
    """Build a request body containing only fields this profile documents.

    The inverse property is the one that matters and is asserted by tests: a
    field belonging to one provider's dialect must never appear in another
    provider's request. Sending DeepSeek's ``thinking`` object to an endpoint
    that has never heard of it is, at best, a 400.
    """
    body: dict[str, Any] = {
        "model": request.model,
        "messages": [
            {"role": message.role, "content": message.content} for message in request.messages
        ],
        profile.max_output_tokens_field: request.max_output_tokens,
        "stream": False,
    }

    if profile.supports_temperature:
        body["temperature"] = request.temperature

    _apply_reasoning(body, request, profile)
    _apply_structured_output(body, request, profile)
    return body


def _apply_reasoning(
    body: dict[str, Any], request: CompletionRequest, profile: ProviderProfile
) -> None:
    if profile.reasoning == "deepseek_thinking":
        # Explicit in both directions: this API defaults to *enabled*, so a
        # missing field would turn the cheap classifier into a reasoning call.
        body["thinking"] = {"type": "enabled" if request.thinking else "disabled"}
        return

    if profile.reasoning == "reasoning_effort":
        if request.thinking:
            # No effort level is invented here. Omitting the field takes the
            # provider's documented default rather than asserting a value this
            # codebase has no basis to choose.
            return
        if profile.minimum_reasoning_effort is not None:
            # This endpoint cannot switch reasoning off, so "as little as the
            # API permits" is the honest translation of thinking=False. The
            # caller still pays for some reasoning tokens; the cost estimate
            # reflects that because it is derived from reported usage, not from
            # what was requested.
            body["reasoning_effort"] = profile.minimum_reasoning_effort
        return

    # profile.reasoning == "none": the endpoint has no reasoning control, so
    # nothing is sent. request.thinking is simply not expressible here.


def _apply_structured_output(
    body: dict[str, Any], request: CompletionRequest, profile: ProviderProfile
) -> None:
    if not request.json_object:
        return

    if profile.structured_output == "json_object":
        body["response_format"] = {"type": "json_object"}
        if profile.requires_json_keyword:
            _assert_prompt_mentions_json(request, profile)
        return

    if profile.structured_output == "json_schema":
        if request.json_schema is None:
            # Refusing beats guessing. A json_schema endpoint given no schema
            # would otherwise silently produce unconstrained prose, and the
            # first sign of it would be a schema violation downstream.
            raise ValueError(
                f"provider {profile.name!r} uses json_schema structured output, "
                "but the request supplied no schema"
            )
        json_schema: dict[str, Any] = {
            "name": request.json_schema_name or "response",
            "schema": request.json_schema,
        }
        if profile.strict_structured_output:
            json_schema["schema"] = strict_json_schema(request.json_schema)
            json_schema["strict"] = True
        body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        return

    # profile.structured_output == "none": the prompt asks for JSON and the
    # caller's schema validation is the gate. Nothing is added to the body.


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a JSON Schema into the subset strict enforcement requires.

    Every object is closed (``additionalProperties: false``) and lists all of
    its properties as ``required``. That is what the strict subset documents,
    and it is stricter than the Pydantic model it came from: a field with a
    default is optional to Pydantic but must still be *emitted* by the model.

    That difference is deliberate and is the point of the exercise. An omitted
    field silently becomes its default, and a default is a real value that the
    rest of the system will act on -- a dedupe confidence of 0.0 suppresses a
    merge exactly as convincingly as a considered one. Requiring the model to
    state every field turns a silent default into an explicit answer.

    The input is not mutated; validation of the reply remains the caller's
    Pydantic model, which is still the only thing that decides what is
    acceptable.
    """
    closed = copy.deepcopy(schema)
    _close_objects(closed)
    return closed


def _close_objects(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict) and properties:
                node["required"] = list(properties)
        for value in node.values():
            _close_objects(value)
    elif isinstance(node, list):
        for value in node:
            _close_objects(value)


def _assert_prompt_mentions_json(request: CompletionRequest, profile: ProviderProfile) -> None:
    """Some endpoints require the word "json" in the prompt when JSON mode is on.

    Checked here rather than trusted to the prompt file, because the failure
    mode without it is a confusing model response rather than an API error.
    """
    if not any("json" in message.content.lower() for message in request.messages):
        raise ValueError(
            f"{profile.name} JSON mode requires the word 'json' to appear in the prompt; "
            "the supplied messages do not mention it"
        )


def parse_completion(
    payload: Any,
    profile: ProviderProfile,
    *,
    latency_ms: int,
    attempts: int,
) -> CompletionResult:
    """Turn a chat-completions response into a :class:`CompletionResult`.

    Raises :class:`ProviderResponseError` when the payload does not match the
    documented shape. An empty or truncated completion is an error here, not a
    silently-empty classification downstream.
    """
    name = profile.name
    if not isinstance(payload, dict):
        raise ProviderResponseError(f"{name}: response was not a JSON object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError(f"{name}: response contained no choices")

    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderResponseError(f"{name}: malformed choice entry")

    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError(f"{name}: choice contained no message")

    content = message.get("content")
    finish_reason = first.get("finish_reason")

    if finish_reason in profile.retryable_finish_reasons:
        # Documented capacity condition: treat exactly like a 5xx.
        raise ProviderUnavailable(f"{name}: finish_reason={finish_reason}")

    if finish_reason == "length":
        # A truncated JSON document would fail schema validation with a confusing
        # message; naming the real cause makes the fix (raise max tokens) obvious.
        raise ProviderResponseError(
            f"{name}: completion hit the output token limit and was truncated"
        )

    if not isinstance(content, str) or not content.strip():
        # JSON mode can occasionally return empty content on several endpoints.
        raise ProviderResponseError(f"{name}: completion content was empty")

    return CompletionResult(
        content=content,
        model=str(payload.get("model") or ""),
        usage=parse_usage(payload.get("usage"), profile),
        finish_reason=str(finish_reason) if finish_reason else None,
        provider_request_id=str(payload.get("id")) if payload.get("id") else None,
        latency_ms=latency_ms,
        attempts=attempts,
        # Recorded as a flag only; the reasoning text itself is discarded here
        # and never reaches storage or the UI.
        had_reasoning_content=(
            bool(message.get(profile.reasoning_content_field))
            if profile.reasoning_content_field
            else False
        ),
    )


def parse_usage(raw_usage: Any, profile: ProviderProfile) -> TokenUsage:
    """Read token accounting in whichever dialect this profile reports it.

    The cache split is not cosmetic: cached and uncached input are priced tens to
    hundreds of times apart, so a cost estimate that ignores the split is
    meaningless. Where a profile reports no split, both counters stay zero and
    :attr:`TokenUsage.billable_cache_miss` bills the whole prompt at the uncached
    rate -- an over-estimate, which is the safe direction for a spend cap.
    """
    if not isinstance(raw_usage, dict):
        return TokenUsage()

    prompt_tokens = _as_int(raw_usage.get("prompt_tokens"))
    details = raw_usage.get("completion_tokens_details")
    reasoning_tokens = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0

    cache_hit = 0
    cache_miss = 0
    if profile.cache_usage == "deepseek_split":
        # Two counters that partition prompt_tokens between them. When only the
        # hit count is reported, the miss count is the remainder -- deriving it
        # rather than leaving it at zero is what keeps the uncached remainder
        # billable. Leaving it zero would credit the whole prompt as cached and
        # under-count spend by roughly thirty times.
        cache_hit = min(prompt_tokens, _as_int(raw_usage.get("prompt_cache_hit_tokens")))
        cache_miss = (
            _as_int(raw_usage.get("prompt_cache_miss_tokens"))
            if "prompt_cache_miss_tokens" in raw_usage
            else prompt_tokens - cache_hit
        )
    elif profile.cache_usage == "openai_details":
        # One counter that is a *subset* of prompt_tokens; the miss count is the
        # remainder. Clamped so a provider reporting more cached than prompt
        # tokens cannot produce a negative billable quantity.
        prompt_details = raw_usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cache_hit = min(prompt_tokens, _as_int(prompt_details.get("cached_tokens")))
            if cache_hit:
                cache_miss = prompt_tokens - cache_hit

    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=_as_int(raw_usage.get("completion_tokens")),
        total_tokens=_as_int(raw_usage.get("total_tokens")),
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        reasoning_tokens=reasoning_tokens,
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


class OpenAICompatibleClient:
    """Async chat-completions client with bounded, classified retries."""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int = 3,
        http: ProviderHttpClient | None = None,
    ) -> None:
        resolved_base_url = (base_url or profile.base_url).strip()
        if not resolved_base_url:
            raise ValueError(
                f"provider {profile.name!r} has no default base URL; one must be configured"
            )

        self.profile = profile
        self.name = profile.name
        self._max_attempts = max(1, max_attempts)
        self._default_timeout = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._http = http or ProviderHttpClient(
            provider=profile.name,
            base_url=resolved_base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout_seconds=self._default_timeout,
            max_attempts=self._max_attempts,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _parse_payload(
        self, payload: dict[str, Any], *, latency_ms: int, attempts: int
    ) -> CompletionResult:
        """Decode one success payload. Subclasses with a different wire
        dialect (``meta-go``: the Responses API) replace this."""
        return parse_completion(payload, self.profile, latency_ms=latency_ms, attempts=attempts)

    async def verify_credentials(self) -> None:
        """Prove the API key is accepted, spending no tokens.

        ``GET /models`` is the OpenAI-compatible catalogue endpoint: it is free,
        it takes no body, and it needs the same bearer token every completion
        needs, so it answers the only question a startup probe should ask of a
        metered LLM -- is this key good?  A completion would answer the same
        question and bill for the privilege.

        Raises ``ProviderAuthError`` for a rejected key, and the usual transport
        errors when the API cannot be reached.
        """
        if self.profile.models_path is None:
            # No free catalogue endpoint. Probing with a completion would bill
            # for a health check, so this provider is simply not probed.
            return
        await self._http.get_json(self.profile.models_path, max_attempts=1)

    def build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Build the request body exactly as this profile's API documents it."""
        return build_body(request, self.profile)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Perform one completion, retrying only clearly retryable failures."""
        profile = self.profile
        body = self.build_body(request)
        started = dt.datetime.now(dt.UTC)
        loop = asyncio.get_running_loop()
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            call_started = loop.time()
            try:
                payload = await self._http.request_json(
                    "POST",
                    profile.chat_path,
                    json_body=body,
                    # A chat completion is not idempotent in the billing sense,
                    # but it has no external side effect: repeating it can only
                    # cost tokens, never place an order. Retries stay bounded and
                    # every attempt is recorded separately in llm_calls.
                    retry_safe=False,
                )
                latency_ms = int((loop.time() - call_started) * 1000)
                result = self._parse_payload(payload, latency_ms=latency_ms, attempts=attempt)
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
                    "llm_retrying",
                    provider=profile.name,
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
                last_error = ProviderUnavailable(f"{profile.name}: {type(exc).__name__}")
                if attempt >= self._max_attempts:
                    break
                await asyncio.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))

        assert last_error is not None
        METRICS.inc(
            "stockbrain_provider_errors_total",
            labels={"provider": profile.name, "kind": "exhausted"},
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


def extract_json(content: str, *, provider: str = "llm") -> Any:
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
            f"{provider}: model output was not valid JSON ({exc.__class__.__name__})"
        ) from exc
