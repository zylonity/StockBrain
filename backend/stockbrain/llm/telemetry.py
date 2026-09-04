"""Persistent LLM call telemetry.

Every attempt is recorded -- successes, failures, and superseded retries --
because an LLM retry can return a different answer, and which result was
actually consumed must be unambiguous afterwards. Exactly one row per logical
operation carries ``used = true``.

No API key, Authorization header or prompt secret is ever written here. The
stored response excerpt is bounded and is model output, not configuration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.research import LlmCall
from stockbrain.llm.base import CompletionResult, TokenUsage
from stockbrain.llm.pricing import PricingTable
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["LlmCallRecord", "LlmTelemetry"]

log = get_logger(__name__)

#: Bound on the stored model-output excerpt. Enough to debug a bad
#: classification, small enough that one runaway response cannot bloat the table.
RESPONSE_EXCERPT_CHARS = 2000


@dataclass(slots=True)
class LlmCallRecord:
    """Everything worth persisting about one attempt."""

    purpose: str
    provider: str
    model: str
    prompt_version: str | None = None
    thinking_enabled: bool = False
    event_id: uuid.UUID | None = None
    research_run_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    attempt: int = 1
    retry_count: int = 0
    succeeded: bool = False
    used: bool = False
    usage: TokenUsage | None = None
    estimated_cost_usd: Decimal | None = None
    latency_ms: int | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None
    had_reasoning_content: bool = False
    error: str | None = None
    error_class: str | None = None
    response_excerpt: str | None = None


class LlmTelemetry:
    """Builds and persists :class:`LlmCall` rows."""

    def __init__(self, pricing: PricingTable | None = None) -> None:
        self._pricing = pricing or PricingTable()

    def from_result(
        self,
        result: CompletionResult,
        *,
        purpose: str,
        provider: str,
        model: str,
        prompt_version: str | None,
        thinking_enabled: bool,
        event_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        used: bool = True,
    ) -> LlmCallRecord:
        cost = self._pricing.estimate(
            result.model or model, result.usage, at=result.completed_at or utcnow()
        )
        return LlmCallRecord(
            purpose=purpose,
            provider=provider,
            model=result.model or model,
            prompt_version=prompt_version,
            thinking_enabled=thinking_enabled,
            event_id=event_id,
            job_id=job_id,
            attempt=result.attempts,
            retry_count=max(0, result.attempts - 1),
            succeeded=True,
            used=used,
            usage=result.usage,
            estimated_cost_usd=cost,
            latency_ms=result.latency_ms,
            started_at=result.started_at,
            completed_at=result.completed_at,
            provider_request_id=result.provider_request_id,
            finish_reason=result.finish_reason,
            had_reasoning_content=result.had_reasoning_content,
            response_excerpt=(result.content or "")[:RESPONSE_EXCERPT_CHARS] or None,
        )

    @staticmethod
    def from_failure(
        error: BaseException,
        *,
        purpose: str,
        provider: str,
        model: str,
        prompt_version: str | None,
        thinking_enabled: bool,
        event_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        started_at: dt.datetime | None = None,
        attempt: int = 1,
    ) -> LlmCallRecord:
        completed = utcnow()
        latency = int((completed - started_at).total_seconds() * 1000) if started_at else None
        return LlmCallRecord(
            purpose=purpose,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            thinking_enabled=thinking_enabled,
            event_id=event_id,
            job_id=job_id,
            attempt=attempt,
            retry_count=max(0, attempt - 1),
            succeeded=False,
            used=False,
            started_at=started_at,
            completed_at=completed,
            latency_ms=latency,
            # Message only. Provider exceptions are constructed without
            # credentials, and the logging layer scrubs configured secrets.
            error=str(error)[:2000],
            error_class=type(error).__name__,
        )

    async def persist(self, session: AsyncSession, record: LlmCallRecord) -> uuid.UUID:
        usage = record.usage or TokenUsage()
        row = LlmCall(
            purpose=record.purpose,
            provider=record.provider,
            model=record.model,
            prompt_version=record.prompt_version,
            thinking_enabled=record.thinking_enabled,
            event_id=record.event_id,
            research_run_id=record.research_run_id,
            job_id=record.job_id,
            attempt=record.attempt,
            retry_count=record.retry_count,
            succeeded=record.succeeded,
            used=record.used,
            input_tokens=usage.prompt_tokens or None,
            output_tokens=usage.completion_tokens or None,
            cached_input_tokens=usage.cache_hit_tokens or None,
            cache_miss_input_tokens=usage.cache_miss_tokens or None,
            reasoning_tokens=usage.reasoning_tokens or None,
            estimated_cost_usd=record.estimated_cost_usd,
            latency_ms=record.latency_ms,
            started_at=record.started_at,
            completed_at=record.completed_at,
            provider_request_id=record.provider_request_id,
            finish_reason=record.finish_reason,
            had_reasoning_content=record.had_reasoning_content,
            error=record.error,
            error_class=record.error_class,
            response_excerpt=record.response_excerpt,
        )
        session.add(row)
        await session.flush()

        if record.estimated_cost_usd is not None:
            METRICS.inc(
                "stockbrain_llm_cost_estimate_usd",
                float(record.estimated_cost_usd),
                labels={"model": record.model, "purpose": record.purpose},
            )
        return row.id

    @staticmethod
    async def spend_since(session: AsyncSession, since: dt.datetime) -> Decimal:
        """Total estimated spend since ``since``.

        Derived from ``llm_calls`` rather than a running counter, so it cannot
        drift from the record of what was actually called and it survives a
        restart with no reconciliation step.
        """
        total = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(LlmCall.estimated_cost_usd), 0)).where(
                    LlmCall.created_at >= since
                )
            )
        ).scalar_one()
        return Decimal(str(total))
