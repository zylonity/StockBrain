"""LLM provider interface.

StockBrain talks to models through this narrow interface, never through a
provider SDK directly, so a provider swap does not reach into the classifier.

The interface is deliberately **text in, text out, plus usage**. It exposes no
tools, no function calling and no callbacks. A model reached through this
interface cannot touch the broker, the filesystem or the network: the only
capability it has is to return a string, which the caller then validates against
a Pydantic schema before anything acts on it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["ChatMessage", "CompletionRequest", "CompletionResult", "LlmProvider", "TokenUsage"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(slots=True)
class TokenUsage:
    """Token accounting from one response.

    Providers that report prompt caching split prompt tokens into cache *hit*
    and *miss* counts, which is not cosmetic: the two are priced tens to
    hundreds of times apart, so a cost estimate that ignores the split is
    meaningless. Each provider spells the split differently; normalising it into
    these two counters is the job of the provider adapter, not of the callers.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def billable_cache_miss(self) -> int:
        """Cache-miss prompt tokens, falling back to the total when unsplit.

        Providers that do not report the split are charged at the miss rate,
        which over-estimates rather than under-estimates. A budget that
        under-counts is worse than one that over-counts.
        """
        if self.cache_hit_tokens or self.cache_miss_tokens:
            return self.cache_miss_tokens
        return self.prompt_tokens


@dataclass(slots=True)
class CompletionRequest:
    """One completion, fully specified.

    ``json_object`` requests provider-native structured output; how that is
    spelled on the wire depends on the provider profile. ``thinking`` is
    explicit rather than defaulted because at least one supported API defaults
    it to *enabled*, and the classifier must run without it wherever the
    provider permits that.
    """

    messages: list[ChatMessage]
    model: str
    max_output_tokens: int = 2000
    temperature: float = 0.0
    json_object: bool = True
    thinking: bool = False
    timeout_seconds: float | None = None
    purpose: str = "UNSPECIFIED"

    json_schema: dict[str, Any] | None = None
    """JSON Schema for providers whose structured output is schema-based.

    Ignored by ``json_object`` providers, which take the schema from the prompt.
    Supplying it is what lets one caller serve both dialects: the schema is
    derived from the same Pydantic model that validates the reply, so the two
    cannot drift apart.
    """

    json_schema_name: str | None = None


@dataclass(slots=True)
class CompletionResult:
    """The raw text a model returned, plus everything needed for telemetry."""

    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    latency_ms: int = 0
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    attempts: int = 1
    had_reasoning_content: bool = False
    """Whether the provider returned hidden reasoning.

    Recorded as a boolean only. The reasoning text itself is never stored or
    surfaced -- the structured ``rationale`` field of the schema is the
    explanation StockBrain shows."""

    raw_response_excerpt: str | None = None


@runtime_checkable
class LlmProvider(Protocol):
    """Minimal capability surface: produce text, report usage."""

    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def aclose(self) -> None: ...


def redact_for_log(value: Any, limit: int = 300) -> str:
    """Short, safe excerpt of a provider payload for logs and telemetry."""
    text = value if isinstance(value, str) else repr(value)
    return text[:limit]
