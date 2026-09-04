"""Research runs, theses, and per-call LLM telemetry.

A :class:`ResearchRun` records one execution of the research engine (the pinned
TradingAgents pipeline behind StockBrain's own ``ResearchEngine`` interface) for
one event/company pair.  A :class:`Thesis` is the normalised, structured output.

No column in this module carries execution parameters.  Position sizing is the
deterministic risk engine's responsibility; the research layer only ever
produces an action, a confidence and an argument.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import ResearchStatus, ThesisAction, TimeHorizon

if TYPE_CHECKING:
    from stockbrain.db.models.companies import Company
    from stockbrain.db.models.sources import Event

__all__ = ["LlmCall", "ResearchRun", "Thesis"]


class ResearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "research_runs"

    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[ResearchStatus] = mapped_column(
        pg_enum(ResearchStatus, "research_status"),
        nullable=False,
        default=ResearchStatus.PENDING,
        server_default=ResearchStatus.PENDING.value,
    )
    trigger: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="EVENT")
    """EVENT | POSITION_REASSESSMENT | MANUAL."""

    as_of: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    """Analysis timestamp.  Tools must not use data published after this instant."""

    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    tradingagents_version: Mapped[str | None] = mapped_column(sa.Text)
    quick_model: Mapped[str | None] = mapped_column(sa.Text)
    deep_model: Mapped[str | None] = mapped_column(sa.Text)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)

    research_packet: Mapped[JSONDict | None] = mapped_column()
    """Exact immutable input handed to the engine, for replay and audit."""

    raw_reports: Mapped[JSONDict | None] = mapped_column()
    structured_decision: Mapped[JSONDict | None] = mapped_column()
    token_usage: Mapped[JSONDict | None] = mapped_column()
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    error: Mapped[str | None] = mapped_column(sa.Text)

    event: Mapped[Event | None] = relationship(back_populates="research_runs")
    company: Mapped[Company] = relationship()
    theses: Mapped[list[Thesis]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.Index("ix_research_runs_event_company", "event_id", "company_id"),
        sa.Index("ix_research_runs_status", "status"),
    )


class Thesis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Normalised research conclusion.

    ``supersedes_thesis_id`` / ``original_thesis_id`` record lineage so that
    "why did we exit?" is answerable from the database alone.
    """

    __tablename__ = "theses"

    research_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[ThesisAction] = mapped_column(
        pg_enum(ThesisAction, "thesis_action"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    time_horizon: Mapped[TimeHorizon] = mapped_column(
        pg_enum(TimeHorizon, "time_horizon"), nullable=False
    )

    summary: Mapped[str | None] = mapped_column(sa.Text)
    bull_case: Mapped[str | None] = mapped_column(sa.Text)
    bear_case: Mapped[str | None] = mapped_column(sa.Text)
    catalysts: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    risks: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))
    invalidation_conditions: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    evidence_source_ids: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    target_logic: Mapped[str | None] = mapped_column(sa.Text)

    original_thesis_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="SET NULL")
    )
    supersedes_thesis_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="SET NULL")
    )

    research_run: Mapped[ResearchRun] = relationship(back_populates="theses")

    __table_args__ = (
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        sa.Index("ix_theses_research_run_id", "research_run_id"),
    )


class LlmCall(UUIDPrimaryKeyMixin, Base):
    """One attempt against an LLM provider.

    Every attempt is persisted -- including failed and superseded ones -- because
    an LLM retry can produce a different answer, and the record of which result
    was actually used must be unambiguous.
    """

    __tablename__ = "llm_calls"

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(sa.Text, nullable=False)
    """CLASSIFY_EVENT | DEDUPE_EVENT | RESEARCH | ..."""

    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="deepseek")
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)
    thinking_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    research_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("jobs.id", ondelete="SET NULL"))

    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    succeeded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    used: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    """True for the single validated result that downstream logic consumed."""

    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    """Prompt tokens served from the provider's cache.

    Kept separate from cache misses because the two are priced roughly thirty
    times apart, so a cost estimate that merges them is meaningless."""

    cache_miss_input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(sa.Integer)

    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)

    provider_request_id: Mapped[str | None] = mapped_column(sa.Text)
    """The provider's own id for the call, for correlating with their support."""

    finish_reason: Mapped[str | None] = mapped_column(sa.Text)
    had_reasoning_content: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    """Whether the provider returned hidden reasoning.

    A flag only. The reasoning text is never stored and never surfaced; the
    structured `rationale` field of the schema is the explanation shown."""

    error: Mapped[str | None] = mapped_column(sa.Text)
    error_class: Mapped[str | None] = mapped_column(sa.Text)
    response_excerpt: Mapped[str | None] = mapped_column(sa.Text)
    """Bounded excerpt of model output. Never a prompt, key or header."""

    __table_args__ = (
        sa.Index("ix_llm_calls_created_at", "created_at"),
        sa.Index("ix_llm_calls_purpose_model", "purpose", "model"),
        sa.Index("ix_llm_calls_event_id", "event_id"),
        sa.Index("ix_llm_calls_job_id", "job_id"),
    )
