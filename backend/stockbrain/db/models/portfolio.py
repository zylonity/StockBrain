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

__all__ = ["BrokerOrder", "PortfolioSnapshot", "Position", "PositionPeak"]


class PortfolioSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "portfolio_snapshots"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    account_id: Mapped[str | None] = mapped_column(sa.Text)
    captured_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    """The account's *primary* currency. Trading 212 reports every value in it
    and does not support multi-currency accounts through the API."""

    cash_available: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    cash_reserved: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    """Cash the broker has already committed to pending orders. Spending it
    twice is exactly the failure the risk engine's cash rule exists to prevent."""

    cash_in_pies: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    invested_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    result_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    total_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))
    broker_environment: Mapped[str | None] = mapped_column(sa.Text)
    """Which broker environment produced this snapshot. A demo balance must
    never size a live order, or the other way round."""

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


class PositionPeak(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Highest broker-reported price seen while a position was open.

    Fed by every account sync, which costs no extra API call.  The price is
    broker-supplied and explicitly not real-time, so this is a *trigger*
    reference only: a proposal's reference price always comes from the
    market-data path via the evaluator.

    The row is deleted with the position it belongs to.  A name closed and
    re-bought is a new position with a new thesis, and inheriting the old
    peak would arm a trailing stop against a high this position never saw.
    """

    __tablename__ = "position_peaks"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="default")
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)

    peak_price: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    peak_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    observations: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    """How many syncs contributed.  A peak from one observation is a peak the
    trailing rule should not yet trust, and this is how it can tell."""

    __table_args__ = (
        sa.UniqueConstraint(
            "broker",
            "account_id",
            "broker_ticker",
            name="uq_position_peaks_broker_account_id_broker_ticker",
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
    """Verbatim broker status string; not coerced into a StockBrain enum.

    Trading 212 documents LOCAL, UNCONFIRMED, CONFIRMED, NEW, CANCELLING,
    CANCELLED, PARTIALLY_FILLED, FILLED, REJECTED, REPLACING and REPLACED. The
    set is the broker's to change, and mapping it onto a StockBrain enum would
    turn an unfamiliar value into either a crash or a wrong guess."""

    broker_environment: Mapped[str | None] = mapped_column(sa.Text)
    """Which environment this order exists in. A demo order and a live order can
    share an id space, so a mirror without this is a mirror of nothing in
    particular."""

    initiated_from: Mapped[str | None] = mapped_column(sa.Text)
    """Trading 212's own account of who created the order: API, IOS, ANDROID,
    WEB, SYSTEM, AUTOINVEST or INSTRUMENT_AUTOINVEST.

    Load-bearing for reconciliation. An order StockBrain placed reads ``API``,
    so an order the operator placed on their phone can be excluded from
    candidate matching rather than being attributed to an ambiguous attempt."""

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
