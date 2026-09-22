"""OpenAI Responses-API client, for endpoints that never learned chat-completions.

StockBrain's LLM layer speaks one dialect -- ``POST /chat/completions`` with a
``messages`` list and ``choices[0].message`` back -- described by a
:class:`~stockbrain.llm.profiles.ProviderProfile`. OpenCode Go's Muse Spark
contributor endpoint (``meta-go``) cannot be described that way: verified live
2026-09-21, ``/chat/completions`` on that base URL is a 503 ("Endpoint is
unavailable") while ``/responses`` answers. This module is the second dialect.
It reuses the shared retry/telemetry machinery in
:class:`~stockbrain.llm.openai_compat.OpenAICompatibleClient` -- attempt rules,
the ``/models`` credential probe and the metrics are not responses-specific --
and overrides only the two steps the wire format differs at.

Structure mirrors the chat dialect exactly: one request builder and one
response parser, both driven by the fields a profile documents, so this module
contains no provider names and no ``if provider ==`` branches. The failure rule
both dialects follow: an ambiguous payload is
:class:`~stockbrain.errors.ProviderResponseError`, never a guess, because
wrong-but-parseable is the worst failure mode available here.

Dialect deltas, all verified live 2026-09-21 against the Go endpoint:

* request -- ``messages`` become ``input`` items; ``max_completion_tokens``
  becomes ``max_output_tokens``; ``response_format={"type": "json_schema",
  ...}`` becomes ``text.format``; ``reasoning_effort`` becomes
  ``{"reasoning": {"effort": ...}}``; chat's wrapped function specs become
  flattened items.
* response -- ``choices[0].message`` becomes the ``output`` item list:
  ``message`` items supply the text parts, ``function_call`` items become tool
  calls, ``reasoning`` items are recorded as a flag only (their content is
  provider-encrypted and never stored), and ``usage`` maps
  ``input_tokens``/``output_tokens`` onto the chat counters, the cache split
  included (``input_tokens_details.cached_tokens`` is a *subset* of
  ``input_tokens``, the same openai-details accounting).

Session: ``x-opencode-session`` is a routing/cache-affinity hint, not a
credential -- the provider answers a hard 400 ("MissingSessionID") without it.
The classifier/dedupe client carries a stable per-process UUID; the research
transport re-scopes it per research run, since one packet's role calls share a
prompt cache and must land on the same backend.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from stockbrain.errors import (
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm.base import CompletionRequest, CompletionResult, TokenUsage
from stockbrain.llm.openai_compat import OpenAICompatibleClient, strict_json_schema
from stockbrain.llm.profiles import ProviderProfile
from stockbrain.logging import get_logger

__all__ = [
    "ResponsesApiClient",
    "build_input",
    "parse_response_payload",
    "parse_responses_usage",
]

log = get_logger(__name__)

#: Responses answers with a ``status`` field, not ``finish_reason``.
_SUCCESS_STATUSES = frozenset({"completed"})
_SESSION_HEADER = "x-opencode-session"


def _message_item(role: str, text: str, part_type: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": part_type, "text": text}]}


def build_input(messages: list[Any], profile: ProviderProfile) -> list[dict[str, Any]]:
    """Convert a chat-turn history into the Responses ``input`` item list.

    Roles carry their text as content-part lists rather than bare strings: both
    spellings are documented, but listing the direction explicitly is what
    keeps the round-trip stable instead of degrading into a bare string that
    each side interprets differently.

    A ``ToolMessage`` is not a chat turn in this dialect: it becomes a
    ``function_call_output`` item keyed by the tool call id, the only spelling
    that keeps a tool result attached to the call that produced it. An
    ``AIMessage`` carrying tool calls becomes ``function_call`` items, so the
    provider's next step survives the round-trip exactly as the chat dialect's
    echoed ``tool_calls`` do.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        # Two caller families share this converter: the classifier's plain
        # ``ChatMessage`` dataclasses (role/content) and the research
        # transport's langchain messages (``role`` in type names, plus
        # ``tool_calls``/``tool_call_id``). Both are accepted here; the
        # conversion below is otherwise identical.
        kind = getattr(message, "type", None) or getattr(message, "role", "")
        # langchain names them "system|human|ai|tool"; chat dataclasses say
        # "system|user|assistant|tool".
        if kind in {"system"}:
            items.append(_message_item("system", _text_of(message.content), "input_text"))
        elif kind in {"human", "user"}:
            items.append(_message_item("user", _text_of(message.content), "input_text"))
        elif kind in {"ai", "assistant"}:
            for call in getattr(message, "tool_calls", None) or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call["id"]),
                        "name": str(call["name"]),
                        "arguments": json.dumps(dict(call["args"])),
                    }
                )
            text = _text_of(message.content)
            if text.strip():
                items.append(_message_item("assistant", text, "output_text"))
        elif kind in {"tool"}:
            output = (
                message.content if isinstance(message.content, str) else json.dumps(message.content)
            )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(getattr(message, "tool_call_id", "") or ""),
                    "output": str(output),
                }
            )
        else:  # pragma: no cover - langchain only produces these four types
            raise ValueError(f"unsupported message type for the Responses API: {kind!r}")
    return items


def _text_of(content: Any) -> str:
    """Content the upstream graph supplies as strings, never part lists."""
    return content if isinstance(content, str) else ""


def _flatten_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Chat's ``{"function": {...}}`` wrapper spelling -> Responses spelling.

    Chat-completions nests the callable's fields under ``"function"``;
    Responses documents the same fields directly on the tool item. Sending the
    wrapped spelling is rejected before any model runs, so the translation is
    by shape here rather than by a list of callers to keep in step.
    """
    spec = tool.get("function")
    if isinstance(spec, dict) and "name" in spec:
        return {
            "type": "function",
            "name": spec["name"],
            **({"description": spec["description"]} if spec.get("description") else {}),
            "parameters": copy.deepcopy(spec.get("parameters", {"type": "object"})),
        }
    return copy.deepcopy(tool)


class ResponsesApiClient(OpenAICompatibleClient):
    """The Responses-API dialect of :class:`OpenAICompatibleClient`.

    The base class owns retry policy, the ``/models`` credential probe,
    metrics and the telemetry plumbing -- none of that is responses-specific.
    This subclass replaces only the two wire-format steps (the request body
    builder and the response parser) and carries the ``x-opencode-session``
    header, a hard provider requirement answered with a 400 ("MissingSessionID")
    when absent. The header is a per-client UUID, not a configuration knob: it
    is a routing/cache-affinity hint, and grouping the classifier's and the
    deduplicator's calls under one prompt-cache session is behaviour, not a
    knob. The research transport re-scopes it per run (one packet, one session).
    """

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int = 3,
        http: ProviderHttpClient | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(
            profile,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            http=http,
        )
        self.session_id = session_id
        if http is None:
            # The responses provider answers every call with a hard 400 when
            # this header is absent (MissingSessionID), so it rides on the
            # client's default headers before the first request leaves.
            self.session_id = self.session_id or str(uuid.uuid4())
            self._client_headers()["x-opencode-session"] = self.session_id

    def _client_headers(self) -> Any:
        """The client's underlying default-header mapping.

        httpx.Headers is a mutable mapping; the accessor on the http wrapper is
        deliberately read-only, so mutation goes to the owner here rather than
        pretending the headers are replaceable.
        """
        return self._http._client.headers

    # -- wire format --------------------------------------------------------

    def build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Build the request body exactly as this profile's API documents it."""
        profile = self.profile
        body: dict[str, Any] = {
            "model": request.model,
            "input": build_input(request.messages, profile),
            profile.max_output_tokens_field: request.max_output_tokens,
            "stream": False,
        }

        if profile.supports_temperature:
            body["temperature"] = request.temperature

        _apply_reasoning(body, request, profile)
        _apply_structured_output(body, request, profile)
        return body

    def _parse_payload(
        self, payload: dict[str, Any], *, latency_ms: int, attempts: int
    ) -> CompletionResult:
        """Parse the Responses payload into the shared :class:`CompletionResult`."""
        return parse_response_payload(
            payload, self.profile, latency_ms=latency_ms, attempts=attempts
        )


def _apply_reasoning(
    body: dict[str, Any], request: CompletionRequest, profile: ProviderProfile
) -> None:
    """The Responses spelling of the reasoning knob, no ``provider`` branches.

    ``{"reasoning": {"effort": ...}}`` is the dialect; the off-switch rule is
    the profile's own. Muse Spark cannot switch reasoning off, so
    ``thinking=False`` lands on the documented floor -- the caller still pays
    for some reasoning, and the cost estimate reflects that because it is
    derived from reported usage, not from what was requested.
    """
    if profile.reasoning != "reasoning_effort":
        # The only reasoning style an endpoint with a Responses shape has
        # shipped so far; this module gains a branch when a profile needs one.
        return
    if request.thinking:
        # No invented default. The transport asserts the tuned effort itself.
        return
    if profile.minimum_reasoning_effort is not None:
        body["reasoning"] = {"effort": profile.minimum_reasoning_effort}


def _apply_structured_output(
    body: dict[str, Any], request: CompletionRequest, profile: ProviderProfile
) -> None:
    """``response_format`` becomes ``text.format`` on this dialect."""
    if not request.json_object:
        return
    if request.json_schema is None:
        # Refusing beats guessing, word for word the chat dialect's rule: a
        # schema-less request on a json_schema endpoint silently produces
        # unconstrained prose, and no one finds out until validation.
        raise ValueError(
            f"provider {profile.name!r} uses json_schema structured output, "
            "but the request supplied no schema"
        )
    declared: dict[str, Any] = {
        "type": "json_schema",
        "name": request.json_schema_name or "response",
        "schema": copy.deepcopy(request.json_schema),
    }
    if profile.strict_structured_output:
        declared["schema"] = strict_json_schema(request.json_schema)
        declared["strict"] = True
    body["text"] = {"format": declared}


def parse_response_payload(
    payload: dict[str, Any],
    profile: ProviderProfile,
    *,
    latency_ms: int,
    attempts: int,
) -> CompletionResult:
    """Turn a Responses-API payload into a :class:`CompletionResult`.

    ``status`` values map onto the chat ``finish_reason`` vocabulary the rest of
    the system already normalises around: ``completed`` -> ``stop``;
    ``incomplete`` with ``max_output_tokens`` -> ``length``, which both parsers
    treat as a truncation the caller must
    fix (```raise the cap``); anything else is an error naming what arrived,
    never a guess.
    """
    if not isinstance(payload, dict):
        raise ProviderResponseError(f"{profile.name}: response was not a JSON object")

    status = payload.get("status")
    if status == "incomplete":
        details = payload.get("incomplete_details") or {}
        reason = details.get("reason") if isinstance(details, dict) else None
        raise ProviderResponseError(
            f"{profile.name}: completion hit the output token limit and was truncated "
            "(incomplete_details.reason=max_output_tokens); fix, raise the cap -- "
            "tokens already billed"
            if reason == "max_output_tokens"
            else f"{profile.name}: response incomplete: {details!r}"
        )
    if status != "completed":
        error = payload.get("error")
        raise ProviderUnavailable(
            f"{profile.name}: response status was {status!r}" + (f": {error!r}" if error else "")
        )

    output = payload.get("output")
    if not isinstance(output, list):
        raise ProviderResponseError(f"{profile.name}: response contained no output list")

    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    had_reasoning = False
    for item in output:
        if not isinstance(item, dict):
            raise ProviderResponseError(f"{profile.name}: malformed output item")
        kind = item.get("type")
        if kind == "message":
            parts = item.get("content")
            if not isinstance(parts, list):
                raise ProviderResponseError(f"{profile.name}: message item had no content list")
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                if part.get("type") == "output_text" and isinstance(text, str):
                    content_parts.append(text)
        elif kind == "function_call":
            tool_calls.append(
                {
                    "name": str(item.get("name") or ""),
                    "args": _decode_arguments(item.get("arguments")),
                    "id": str(item.get("call_id") or ""),
                    "type": "tool_call",
                }
            )
        elif kind == "reasoning":
            # Recorded as a flag only, per both parsers' data-use rule; the
            # provider encrypts the content on this endpoint in any case.
            had_reasoning = True
        else:  # pragma: no cover - new item types are added to the parser too
            raise ProviderResponseError(f"{profile.name}: unknown output item type {kind!r}")

    content = "".join(content_parts)
    if not content.strip() and not tool_calls:
        # JSON mode can occasionally return empty content on several endpoints;
        # an empty first turn is an error here, not a downstream empty column.
        raise ProviderResponseError(f"{profile.name}: completion content was empty")

    usage = parse_responses_usage(payload.get("usage"), profile)

    return CompletionResult(
        content=content,
        model=str(payload.get("model") or ""),
        usage=usage,
        finish_reason="stop" if not tool_calls else "tool_calls",
        provider_request_id=str(payload.get("id")) if payload.get("id") else None,
        latency_ms=latency_ms,
        attempts=attempts,
        had_reasoning_content=had_reasoning,
    )


def parse_responses_usage(raw_usage: Any, profile: ProviderProfile) -> TokenUsage:
    """Map the Responses ``usage`` dialect onto the chat-dialect counters.

    The cache split keeps openai-details semantics: ``cached_tokens`` is a
    subset of ``input_tokens``, so the miss count is the remainder, clamped
    against a provider that reports more cached than prompt tokens (a negative
    billable quantity is not an accounting concept, it is a bug).
    """
    if not isinstance(raw_usage, dict) or not raw_usage:
        return TokenUsage()

    input_tokens = _as_int(raw_usage.get("input_tokens"))
    cached = 0
    details = raw_usage.get("input_tokens_details")
    if isinstance(details, dict):
        cached = min(input_tokens, _as_int(details.get("cached_tokens")))
    reasoning = 0
    out_details = raw_usage.get("output_tokens_details")
    if isinstance(out_details, dict):
        reasoning = _as_int(out_details.get("reasoning_tokens"))
    return TokenUsage(
        prompt_tokens=input_tokens,
        completion_tokens=_as_int(raw_usage.get("output_tokens")),
        total_tokens=_as_int(raw_usage.get("total_tokens")),
        cache_hit_tokens=cached,
        cache_miss_tokens=input_tokens - cached if cached else input_tokens,
        reasoning_tokens=reasoning,
    )


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """Decode function-call arguments, a JSON string by contract.

    An exception here means the payload is not the documented contract; the
    caller converts it into :class:`ProviderResponseError` along with the rest
    of the malformed-payload cases.
    """
    if not isinstance(raw, str):
        raise ValueError("function_call arguments were not a JSON string")
    try:
        decoded = json.loads(raw or "{}")
    except ValueError as exc:
        raise ValueError(f"function_call arguments were not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("function_call arguments decoded to a non-object")
    return decoded


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
