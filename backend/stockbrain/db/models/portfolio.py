"""Local mirror of broker account state.

These tables are *derived* state: the broker remains authoritative.  They exist
so that risk checks, the GUI and reconciliation read a consistent snapshot
without hammering rate-limited endpoints, and so that historical account state
is auditable after the fact.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import Broker, OrderSide, OrderType

__all__ = ["BrokerOrder", "PortfolioSnapshot", "Position"]


class PortfolioSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "portfolio_snapshots"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    account_id: Mapped[str | None] = mapped_column(sa.Text)
    captured_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    cash_available: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    invested_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    result_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    total_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    raw: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (sa.Index("ix_portfolio_snapshots_broker_captured", "broker", "captured_at"),)


class Position(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Latest known position per broker instrument."""

    __tablename__ = "positions"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="default")
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="SET NULL")
    )

    quantity: Mapped[Decimal] = mapped_column(sa.Numeric(28, 10), nullable=False)
    quantity_available: Mapped[Decimal | None] = mapped_column(sa.Numeric(28, 10))
    average_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    current_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    """Broker-supplied price. Display/reconciliation only -- explicitly not real-time."""

    ppl: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    initial_fill_date: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_synced_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    raw: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        sa.UniqueConstraint(
            "broker",
            "account_id",
            "broker_ticker",
            name="uq_positions_broker_account_id_broker_ticker",
        ),
    )


class BrokerOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Normalised local mirror of an order known to the broker.

    Rows are created either from a submission response or from reconciliation
    discovering an order StockBrain did not have a definitive response for.
    """

    __tablename__ = "broker_orders"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="SET NULL")
    )
    execution_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("execution_attempts.id", ondelete="SET NULL")
    )

    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    side: Mapped[OrderSide] = mapped_column(pg_enum(OrderSide, "order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(pg_enum(OrderType, "order_type"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(sa.Numeric(28, 10), nullable=False)
    filled_quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(28, 10))
    limit_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    stop_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    filled_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    currency: Mapped[str | None] = mapped_column(sa.String(3))

    broker_status: Mapped[str | None] = mapped_column(sa.Text)
    """Verbatim broker status string; not coerced into a StockBrain enum."""

    is_terminal: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    discovered_by_reconciliation: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    submitted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_synced_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    raw: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        sa.UniqueConstraint(
            "broker", "broker_order_id", name="uq_broker_orders_broker_broker_order_id"
        ),
        sa.Index("ix_broker_orders_proposal_id", "proposal_id"),
        sa.Index("ix_broker_orders_ticker", "broker_ticker"),
    )
