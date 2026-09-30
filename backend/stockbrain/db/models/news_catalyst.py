"""Immutable entry-time catalyst expectation and one deadline review per BUY fill."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin


class NewsCatalystDeadline(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "news_catalyst_deadlines"

    broker_order_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("broker_orders.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    market_symbol: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entry_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    entry_time_basis: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expected_threshold: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 8))
    supporting_sample_size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    response_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    nonresponse_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    deadline_sessions_after_entry: Mapped[int | None] = mapped_column(sa.Integer)
    buffer_sessions: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    measurement_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_snapshot: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    reassessment_requested_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    reassessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="SET NULL")
    )
    observed_market_relative_move: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 8))
    review_reason: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint("supporting_sample_size >= 0", name="news_catalyst_nonnegative_sample"),
        sa.CheckConstraint("response_count >= 0", name="news_catalyst_nonnegative_response"),
        sa.CheckConstraint("nonresponse_count >= 0", name="news_catalyst_nonnegative_nonresponse"),
        sa.CheckConstraint("buffer_sessions >= 0", name="news_catalyst_nonnegative_buffer"),
        sa.CheckConstraint(
            "deadline_sessions_after_entry IS NULL OR deadline_sessions_after_entry >= 0",
            name="news_catalyst_nonnegative_deadline",
        ),
        sa.Index("ix_news_catalyst_pending", "reassessment_requested_at"),
    )
