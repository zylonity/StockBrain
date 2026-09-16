"""Thesis outcomes: the ground truth research is graded against.

One ``thesis_outcomes`` row per EXECUTED thesis-backed proposal, one
``thesis_outcome_grades`` row per (outcome, checkpoint).  Written only by the
memory sweep; read by the research packet and the risk engine.  No LLM output
is stored here -- every column is a number, a date or an identifier.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import Broker, OutcomeStatus, ThesisAction, TimeHorizon

__all__ = ["ThesisOutcome", "ThesisOutcomeGrade"]


class ThesisOutcome(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "thesis_outcomes"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    thesis_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="CASCADE"), nullable=False
    )
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    broker_instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("broker_instruments.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(sa.Text)
    action: Mapped[ThesisAction] = mapped_column(
        pg_enum(ThesisAction, "thesis_action"), nullable=False
    )
    horizon: Mapped[TimeHorizon] = mapped_column(
        pg_enum(TimeHorizon, "time_horizon"), nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(sa.Numeric(4, 3), nullable=False)
    is_exit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    exit_rule_id: Mapped[str | None] = mapped_column(sa.Text)
    entry_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    entry_date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    reference_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    benchmark_symbol: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[OutcomeStatus] = mapped_column(
        pg_enum(OutcomeStatus, "outcome_status"),
        nullable=False,
        server_default=OutcomeStatus.PENDING.value,
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(sa.Text)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    """When the sweep last fetched bars for this outcome; the 20-hour clock."""

    grades: Mapped[list[ThesisOutcomeGrade]] = relationship(
        back_populates="outcome",
        cascade="all, delete-orphan",
        order_by="ThesisOutcomeGrade.graded_at",
    )

    __table_args__ = (
        sa.Index("ix_thesis_outcomes_status", "status"),
        sa.Index("ix_thesis_outcomes_company_action", "company_id", "action"),
        sa.Index("ix_thesis_outcomes_event_type_action", "event_type", "action"),
        sa.Index("ix_thesis_outcomes_broker_ticker", "broker", "broker_ticker"),
    )


class ThesisOutcomeGrade(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "thesis_outcome_grades"

    outcome_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("thesis_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    checkpoint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    trading_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    entry_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    current_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    benchmark_entry_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    benchmark_current_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    instrument_return: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    benchmark_return: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    alpha: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    correct: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    graded_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    outcome: Mapped[ThesisOutcome] = relationship(back_populates="grades")

    __table_args__ = (
        sa.UniqueConstraint("outcome_id", "checkpoint", name="uq_thesis_outcome_grades_checkpoint"),
        sa.Index("ix_thesis_outcome_grades_graded_at", "graded_at"),
    )
