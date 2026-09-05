"""DeepSeek research transport: hidden reasoning lives only in in-memory messages.

Use upstream's dedicated serializer/parser, but StockBrain's HTTP and telemetry.
Calling the transport directly avoids upstream SDK retries and tracing callbacks.
The classification LlmProvider remains text-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

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
from stockbrain.llm.telemetry import LlmCallRecord, LlmTelemetry


class CompletionDetails(BaseModel):
    reasoning_tokens: int = Field(default=0, ge=0)


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_cache_hit_tokens: int = Field(default=0, ge=0)
    prompt_cache_miss_tokens: int = Field(default=0, ge=0)
    completion_tokens_details: CompletionDetails = Field(default_factory=CompletionDetails)

    @model_validator(mode="after")
    def validate_accounting(self) -> Usage:
        if "prompt_cache_miss_tokens" not in self.model_fields_set:
            self.prompt_cache_miss_tokens = self.prompt_tokens - self.prompt_cache_hit_tokens
        if (
            self.prompt_cache_miss_tokens < 0
            or self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens != self.prompt_tokens
            or self.completion_tokens_details.reasoning_tokens > self.completion_tokens
            or self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("inconsistent token accounting")
        return self


class ResearchTransport:
    def __init__(
        self,
        key: SecretStr,
        *,
        timeout: float = 120,
        http: ProviderHttpClient | None = None,
        max_tokens: int = 3000,
    ) -> None:
        self.http = http or ProviderHttpClient(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            headers={"Authorization": "Bearer " + key.get_secret_value()},
            timeout_seconds=timeout,
            max_attempts=1,
        )
        self.max_tokens = max_tokens
        self.telemetry = LlmTelemetry()
        self._serializers: dict[str, Any] = {}

    def serializer(self, model: str) -> Any:
        if model not in self._serializers:
            cls = upstream_module("llm_clients.openai_client").DeepSeekChatOpenAI
            # This object only serializes/parses; it never performs HTTP or gets a real key.
            self._serializers[model] = cls(
                model=model,
                api_key="transport-managed",
                base_url="https://api.deepseek.com",
                max_retries=0,
                streaming=False,
            )
        return self._serializers[model]

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
        json_object: bool = False,
    ) -> AIMessage:
        serializer = self.serializer(model)
        body: dict[str, Any] = serializer._get_request_payload(messages)
        body.update(
            max_tokens=self.max_tokens,
            stream=False,
            thinking={"type": "enabled" if thinking else "disabled"},
        )
        if tools:
            body["tools"] = tools
        if json_object:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(1, 3):
            await check_budget()
            started = utcnow()
            record: LlmCallRecord | None = None
            try:
                # Retry is explicit here so every attempt is accounted for separately.
                payload = await self.http.request_json("POST", "/chat/completions", json_body=body)
                if not isinstance(payload, dict):
                    raise ProviderResponseError("deepseek research: malformed response")
                try:
                    usage = Usage.model_validate(payload.get("usage"))
                    choice = payload["choices"][0]
                    message = choice["message"]
                    finish = choice["finish_reason"]
                    if finish not in {
                        "stop",
                        "tool_calls",
                        "length",
                        "content_filter",
                        "insufficient_system_resource",
                    }:
                        finish = "unknown"
                    reported_model = payload["model"]
                    if not isinstance(message, dict) or reported_model != model:
                        raise ValueError("unexpected model or message")
                    result = CompletionResult(
                        content="",
                        model=reported_model,
                        usage=TokenUsage(
                            prompt_tokens=usage.prompt_tokens,
                            completion_tokens=usage.completion_tokens,
                            total_tokens=usage.total_tokens,
                            cache_hit_tokens=usage.prompt_cache_hit_tokens,
                            cache_miss_tokens=usage.prompt_cache_miss_tokens,
                            reasoning_tokens=usage.completion_tokens_details.reasoning_tokens,
                        ),
                        finish_reason=str(finish),
                        provider_request_id=str(payload.get("id") or ""),
                        started_at=started,
                        completed_at=utcnow(),
                        latency_ms=int((utcnow() - started).total_seconds() * 1000),
                        had_reasoning_content=bool(message.get("reasoning_content")),
                    )
                except (ValidationError, KeyError, IndexError, TypeError, ValueError):
                    raise ProviderResponseError("deepseek research: malformed response") from None
                record = self.telemetry.from_result(
                    result,
                    purpose=f"RESEARCH:{role}",
                    provider="deepseek",
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    thinking_enabled=thinking,
                    used=False,
                )
                record.attempt = attempt
                record.retry_count = attempt - 1
                if finish == "insufficient_system_resource":
                    raise ProviderUnavailable("deepseek research: insufficient_system_resource")
                if finish not in {"stop", "tool_calls"}:
                    raise ProviderResponseError(f"deepseek research: finish_reason={finish}")
                if not message.get("content") and not message.get("tool_calls"):
                    raise ProviderResponseError("deepseek research: empty response")
                try:
                    response = serializer._create_chat_result(payload).generations[0].message
                except Exception:
                    raise ProviderResponseError(
                        "deepseek research: invalid assistant message"
                    ) from None
                if not isinstance(response, AIMessage) or response.invalid_tool_calls:
                    raise ResearchValidationError("deepseek research: malformed tool call")
                record.used = True
                await record_call(record)
                return response
            except asyncio.CancelledError:
                # No automatic replay: a cancelled HTTP request might already have been billed.
                failure = self.telemetry.from_failure(
                    RuntimeError("research call cancelled; provider spend may be unknown"),
                    purpose=f"RESEARCH:{role}",
                    provider="deepseek",
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
                        provider="deepseek",
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
                # Only an explicit capacity response is repeated automatically. Transport
                # failures have uncertain spend, so the run fails visibly instead.
                if (
                    isinstance(exc, ProviderUnavailable)
                    and record.finish_reason == "insufficient_system_resource"
                    and attempt < 2
                ):
                    await asyncio.sleep(0.5)
                    continue
                raise safe_research_error(exc) from None
        raise ProviderUnavailable("deepseek research: retry budget exhausted")

    async def aclose(self) -> None:
        await self.http.aclose()
