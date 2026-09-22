"""Research transport: hidden reasoning lives only in in-memory messages.

Use upstream's dedicated serializer/parser, but StockBrain's HTTP and telemetry.
Calling the transport directly avoids upstream SDK retries and tracing callbacks.
The classification LlmProvider remains text-only.

The wire format is whatever the configured
:class:`~stockbrain.llm.profiles.ProviderProfile` documents, so the same
TradingAgents graph runs against any OpenAI-compatible endpoint. Only two things
are provider-specific here and both are profile-driven: which upstream
serializer class is used (DeepSeek's thinking models require reasoning content to
be echoed back on the next turn; nothing else does), and which extra generation
fields the body carries.

Reasoning content is never fabricated for a provider that does not return it.
Where a profile reports none, ``had_reasoning_content`` is simply ``False`` and
the message round-trips without it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import SecretStr

from stockbrain.db.base import utcnow
from stockbrain.errors import ProviderResponseError, ProviderUnavailable
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research import (
    PROMPT_VERSION,
    CheckBudget,
    RecordCall,
    ResearchValidationError,
    safe_research_error,
)
from stockbrain.intelligence.tradingagents_runtime import upstream_module
from stockbrain.llm.base import CompletionResult, TokenUsage
from stockbrain.llm.openai_compat import parse_usage, strict_json_schema
from stockbrain.llm.pricing import PricingTable
from stockbrain.llm.profiles import DEEPSEEK, ProviderProfile
from stockbrain.llm.responses_api import (
    _flatten_tool,
    build_input,
    parse_responses_usage,
)
from stockbrain.llm.telemetry import LlmCallRecord, LlmTelemetry
from stockbrain.logging import get_logger

#: Finish reasons that are understood. Anything else is normalised to
#: ``unknown`` so an unfamiliar value cannot be mistaken for a clean stop.
log = get_logger(__name__)

_KNOWN_FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "content_filter"})

#: Finish reasons that mean the model produced a usable answer.
_SUCCESS_FINISH_REASONS = frozenset({"stop", "tool_calls"})


def validate_usage(usage: TokenUsage) -> None:
    """Reject token accounting that does not add up.

    The transport fails closed on inconsistent usage rather than recording a
    cost derived from numbers that contradict each other. The cache split is
    checked only when the provider reported one: a profile that reports no split
    leaves both counters at zero, and zero is not evidence of a cache hit.
    """
    if usage.total_tokens != usage.prompt_tokens + usage.completion_tokens:
        raise ValueError("inconsistent token accounting: total != prompt + completion")
    if usage.reasoning_tokens > usage.completion_tokens:
        raise ValueError("inconsistent token accounting: reasoning > completion")
    reported_split = usage.cache_hit_tokens or usage.cache_miss_tokens
    if reported_split and usage.cache_hit_tokens + usage.cache_miss_tokens != usage.prompt_tokens:
        raise ValueError("inconsistent token accounting: cache split != prompt")


class ResearchTransport:
    def __init__(
        self,
        key: SecretStr,
        *,
        profile: ProviderProfile = DEEPSEEK,
        base_url: str | None = None,
        timeout: float = 120,
        http: ProviderHttpClient | None = None,
        max_tokens: int = 3000,
        pricing: PricingTable | None = None,
        deep_reasoning_effort: str = "low",
    ) -> None:
        self.profile = profile
        #: ``reasoning_effort`` for the thinking roles on a profile that has the
        #: knob. Left unspecified, Muse Spark chose its own -- about 2,300
        #: reasoning tokens on a bear call -- and the p99 manager call ran to
        #: 109s against a 120s timeout. Measured on three stored packets, ``low``
        #: reached the same action, horizon and confidence (+-0.02) in ~45% of
        #: the time; the risk lists lost their fourth and fifth entries.
        self.deep_reasoning_effort = deep_reasoning_effort
        self.base_url = (base_url or profile.base_url).strip()
        if not self.base_url:
            raise ValueError(
                f"provider {profile.name!r} has no default base URL; one must be configured"
            )
        self.http = http or ProviderHttpClient(
            provider=profile.name,
            base_url=self.base_url,
            headers={"Authorization": "Bearer " + key.get_secret_value()},
            timeout_seconds=timeout,
            max_attempts=1,
        )
        self.max_tokens = max_tokens
        self.telemetry = LlmTelemetry(pricing)
        self._serializers: dict[str, Any] = {}

    def serializer(self, model: str) -> Any:
        """Upstream serializer for this provider's dialect.

        DeepSeek's thinking models require ``reasoning_content`` from prior
        assistant turns to be echoed back, which is why they get a dedicated
        upstream subclass. Every other endpoint uses the plain normalized client:
        inventing a reasoning field for a provider that does not produce one
        would corrupt the message history rather than preserve continuity.
        """
        if model not in self._serializers:
            module = upstream_module("llm_clients.openai_client")
            cls = (
                module.DeepSeekChatOpenAI
                if self.profile.name == DEEPSEEK.name
                else module.NormalizedChatOpenAI
            )
            # This object only serializes/parses; it never performs HTTP or gets a real key.
            self._serializers[model] = cls(
                model=model,
                api_key="transport-managed",
                base_url=self.base_url,
                max_retries=0,
                streaming=False,
            )
        return self._serializers[model]

    def build_body(
        self,
        messages: list[BaseMessage],
        *,
        model: str,
        thinking: bool,
        tools: list[dict[str, Any]] | None,
        json_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Serialize messages and add only the fields this profile documents."""
        if self.profile.api_style == "responses":
            return self._build_responses_body(
                messages, model=model, thinking=thinking, tools=tools, json_schema=json_schema
            )
        profile = self.profile
        serializer = self.serializer(model)
        body: dict[str, Any] = serializer._get_request_payload(messages)
        # Upstream serializes `max_tokens`; rename it when the profile documents
        # a different field, so no endpoint receives another's spelling.
        body.pop("max_tokens", None)
        body[profile.max_output_tokens_field] = self.max_tokens
        body["stream"] = False

        if profile.reasoning == "deepseek_thinking":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif profile.reasoning == "reasoning_effort":
            if thinking:
                body["reasoning_effort"] = self.deep_reasoning_effort
            elif profile.minimum_reasoning_effort is not None:
                body["reasoning_effort"] = profile.minimum_reasoning_effort

        if tools:
            body["tools"] = tools
        if json_schema is not None:
            # Without this branch a json_schema provider received no
            # ``response_format`` at all and the final decision was free text that
            # merely tended to be JSON -- which is what ``ResearchValidationError``
            # was recording after all of a run's calls had already been paid for.
            if profile.structured_output == "json_schema":
                declared: dict[str, Any] = {"name": "research_decision", "schema": json_schema}
                if profile.strict_structured_output:
                    declared["schema"] = strict_json_schema(json_schema)
                    declared["strict"] = True
                body["response_format"] = {"type": "json_schema", "json_schema": declared}
            elif profile.structured_output == "json_object":
                body["response_format"] = {"type": "json_object"}
        return body

    def _build_responses_body(
        self,
        messages: list[BaseMessage],
        *,
        model: str,
        thinking: bool,
        tools: list[dict[str, Any]] | None,
        json_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The same telemetrized body for a Responses-API research call.

        Message conversion is shared with the classifier client
        (:func:`build_input`) rather than reinvented here: the boundary between
        "chat turn" and "input item" is a wire decision, not a caller decision,
        and the two callers must not drift apart. What is research-specific and
        therefore lives here: the tuned ``reasoning`` effort for the thinking
        roles, the tool re-spelling, and the strict decision schema under
        ``text.format``.
        """
        profile = self.profile
        body: dict[str, Any] = {
            "model": model,
            "input": build_input(messages, profile),
            "stream": False,
        }
        body[profile.max_output_tokens_field] = self.max_tokens

        # Same effort policy as the chat dialect, same rationale (see
        # ``deep_reasoning_effort``): omitted, the endpoint picks its own.
        if thinking:
            body["reasoning"] = {"effort": self.deep_reasoning_effort}
        elif profile.minimum_reasoning_effort is not None:
            body["reasoning"] = {"effort": profile.minimum_reasoning_effort}

        if tools:
            # Chat's wrapped function specs are a different dialect's spelling;
            # the responses provider rejects the wrapper before any model runs.
            body["tools"] = [_flatten_tool(tool) for tool in tools]
        if json_schema is not None:
            if profile.structured_output != "json_schema":
                raise ValueError(
                    f"{profile.name!r} uses a Responses endpoint but not json_schema "
                    "structured output; no fallback is documented"
                )
            declared: dict[str, Any] = {
                "type": "json_schema",
                "name": "research_decision",
                "schema": json_schema,
            }
            if profile.strict_structured_output:
                declared["schema"] = strict_json_schema(json_schema)
                declared["strict"] = True
            body["text"] = {"format": declared}
        return body

    async def complete(
        self,
        messages: list[BaseMessage],
        *,
        role: str,
        model: str,
        thinking: bool,
        record_call: RecordCall,
        check_budget: CheckBudget,
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AIMessage:
        profile = self.profile
        name = profile.name
        # Responses endpoints only: one research run carries its session id so
        # all of a run's calls land in the provider's same prompt-cache group
        # (``x-opencode-session`` is a routing hint, not a credential). A chat
        # profile never sets the header at all -- cast nothing onto an endpoint
        # that has never heard of it.
        session_headers = (
            {"x-opencode-session": session_id}
            if session_id and profile.api_style == "responses"
            else None
        )
        serializer = self.serializer(model) if profile.api_style == "chat_completions" else None
        body = self.build_body(
            messages, model=model, thinking=thinking, tools=tools, json_schema=json_schema
        )
        for attempt in range(1, 3):
            await check_budget()
            started = utcnow()
            record: LlmCallRecord | None = None
            try:
                # Retry is explicit here so every attempt is accounted for separately.
                payload = await self.http.request_json(
                    "POST",
                    profile.chat_path,
                    json_body=body,
                    headers=session_headers,
                )
                if not isinstance(payload, dict):
                    raise ProviderResponseError(f"{name} research: malformed response")
                try:
                    if profile.api_style == "responses":
                        usage, choice_message, finish, reported_model, had_reasoning, tool_calls = (
                            _decode_responses(payload, model, profile)
                        )
                    else:
                        usage = parse_usage(payload.get("usage"), profile)
                        validate_usage(usage)
                        choice = payload["choices"][0]
                        message = choice["message"]
                        choice_message = message
                        finish = choice["finish_reason"]
                        if finish not in _KNOWN_FINISH_REASONS | profile.retryable_finish_reasons:
                            finish = "unknown"
                        reported_model = payload["model"]
                        if not isinstance(message, dict) or reported_model != model:
                            raise ValueError("unexpected model or message")
                        had_reasoning = (
                            bool(message.get(profile.reasoning_content_field))
                            if profile.reasoning_content_field
                            else False
                        )
                        tool_calls = list(message.get("tool_calls") or [])
                    result = CompletionResult(
                        content="",
                        model=reported_model,
                        usage=usage,
                        finish_reason=str(finish),
                        provider_request_id=str(payload.get("id") or ""),
                        started_at=started,
                        completed_at=utcnow(),
                        latency_ms=int((utcnow() - started).total_seconds() * 1000),
                        had_reasoning_content=had_reasoning,
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ProviderResponseError(f"{name} research: malformed response") from None
                record = self.telemetry.from_result(
                    result,
                    purpose=f"RESEARCH:{role}",
                    provider=name,
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    thinking_enabled=thinking,
                    used=False,
                )
                record.attempt = attempt
                record.retry_count = attempt - 1
                if finish in profile.retryable_finish_reasons:
                    raise ProviderUnavailable(f"{name} research: {finish}")
                if finish == "length" and not choice_message.get("content"):
                    # Meta returns ``content: null`` on a capped completion and
                    # bills every token of the discarded draft.  Naming that
                    # keeps it from being read as a model that said nothing.
                    log.warning(
                        "research_response_discarded_at_cap",
                        provider=name,
                        role=role,
                        cap_field=profile.max_output_tokens_field,
                        cap=self.max_tokens,
                        completion_tokens=usage.completion_tokens,
                    )
                    raise ProviderResponseError(
                        f"{name} research: response discarded by the provider at "
                        f"{profile.max_output_tokens_field}={self.max_tokens}; tokens billed"
                    )
                if finish not in _SUCCESS_FINISH_REASONS:
                    raise ProviderResponseError(f"{name} research: finish_reason={finish}")
                if not choice_message.get("content") and not tool_calls:
                    raise ProviderResponseError(f"{name} research: empty response")
                if serializer is not None:
                    try:
                        response = serializer._create_chat_result(payload).generations[0].message
                    except Exception:
                        raise ProviderResponseError(
                            f"{name} research: invalid assistant message"
                        ) from None
                else:
                    # The Responses dialect has no upstream serializer to borrow;
                    # the already-validated decode produces the AIMessage itself.
                    response = AIMessage(
                        content=str(choice_message.get("content") or ""),
                        tool_calls=list(tool_calls or []),
                    )
                if not isinstance(response, AIMessage) or response.invalid_tool_calls:
                    raise ResearchValidationError(f"{name} research: malformed tool call")
                record.used = True
                await record_call(record)
                return response
            except asyncio.CancelledError:
                # No automatic replay: a cancelled HTTP request might already have been billed.
                failure = self.telemetry.from_failure(
                    RuntimeError("research call cancelled; provider spend may be unknown"),
                    purpose=f"RESEARCH:{role}",
                    provider=name,
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    thinking_enabled=thinking,
                    started_at=started,
                )
                await record_call(failure)
                raise
            except Exception as exc:
                if record is None:
                    record = self.telemetry.from_failure(
                        RuntimeError(type(exc).__name__),
                        purpose=f"RESEARCH:{role}",
                        provider=name,
                        model=model,
                        prompt_version=PROMPT_VERSION,
                        thinking_enabled=thinking,
                        started_at=started,
                        attempt=attempt,
                    )
                record.succeeded = False
                record.error_class = type(exc).__name__
                record.error = type(exc).__name__
                await record_call(record)
                # Repeated automatically: an explicit capacity response, and one
                # transport failure.  A read timeout has uncertain spend, but the
                # run it sits in has already paid for up to eight other calls;
                # one retry is cheaper than discarding them.  Every attempt is
                # its own llm_calls row either way.
                retryable = (
                    isinstance(exc, ProviderUnavailable)
                    and record.finish_reason in profile.retryable_finish_reasons
                ) or _is_transport_failure(exc)
                if retryable and attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                raise safe_research_error(exc) from None
        raise ProviderUnavailable(f"{name} research: retry budget exhausted")

    async def aclose(self) -> None:
        await self.http.aclose()


def _decode_responses(
    payload: dict[str, Any],
    model: str,
    profile: ProviderProfile,
) -> tuple[TokenUsage, dict[str, Any], str, str, bool, list[dict[str, Any]]]:
    """Decode the Responses dialect into the same values as a chat choice.

    Returns ``(usage, message_view, finish, model, had_reasoning, tool_calls)``.
    ``message_view`` is a chat-shaped view of the output: the joined
    ``output_text`` parts plus ``tool_calls`` when present, which is the shape
    every downstream consumer of a transport result already speaks. Both
    dialect paths decode tool-call arguments and build the AIMessage exactly
    once, so there is no unshared secondary parser to fall out of step.
    """
    status = payload.get("status")
    finish = "stop" if status == "completed" else "unknown"
    if status == "incomplete":
        details = payload.get("incomplete_details") or {}
        if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
            # Same treatment as the chat dialect's ``finish_reason=length``:
            # the truncation branch below names the real cause; tokens billed.
            finish = "length"
    reported_model = payload.get("model")
    if reported_model != model or not isinstance(payload.get("output"), list):
        raise ValueError("unexpected model or output list")

    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    had_reasoning = False
    for item in payload["output"]:
        if not isinstance(item, dict):
            raise ValueError("malformed output item")
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        content_parts.append(text)
        elif kind == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                raise ValueError("function_call arguments were not valid JSON") from None
            if not isinstance(args, dict):
                raise ValueError("function_call arguments decoded to a non-object")
            tool_calls.append(
                {
                    "name": str(item.get("name") or ""),
                    "args": args,
                    "id": str(item.get("call_id") or ""),
                    "type": "tool_call",
                }
            )
        elif kind == "reasoning":
            # A flag only; the reasoning text itself (provider-encrypted here)
            # is never persisted.
            had_reasoning = True
        else:
            raise ValueError(f"unexpected output item type {kind!r}")

    usage = parse_responses_usage(payload.get("usage"), profile)
    validate_usage(usage)
    message_view = {"content": "".join(content_parts), "tool_calls": tool_calls}
    return (usage, message_view, finish, str(reported_model or ""), had_reasoning, tool_calls)


def _is_transport_failure(exc: BaseException) -> bool:
    """The http client wraps every ``httpx.HTTPError`` in this one message shape."""
    return isinstance(exc, ProviderUnavailable) and "transport failure" in str(exc)
