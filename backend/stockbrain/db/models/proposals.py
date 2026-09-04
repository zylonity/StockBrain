"""Trade proposals, human approval actions, and broker execution attempts.

This module carries the system's strongest safety invariants, and several of
them are enforced by PostgreSQL rather than by application code:

* ``uq_execution_attempts_sent_once`` -- a partial unique index on
  ``(proposal_id) WHERE sent_to_broker`` guarantees that **at most one**
  execution attempt per proposal can ever have transmitted an order to the
  broker.  Because the Trading 212 order POST is documented as non-idempotent,
  this is the last line of defence against a duplicate order: even a bug that
  bypassed every service-layer check would hit a unique-violation before a
  second request could be recorded as sent.
* ``uq_trade_proposals_active_instrument`` -- at most one proposal per broker
  instrument may be in a non-terminal state at a time.
* ``version`` is a SQLAlchemy optimistic-lock column, layered on top of the
  ``SELECT ... FOR UPDATE`` used in the approval path, so a Telegram confirm and
  a web confirm racing on the same proposal cannot both win.
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
from stockbrain.enums import (
    ApprovalChannel,
    ApprovalStage,
    Broker,
    ExecutionOutcome,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
)

if TYPE_CHECKING:
    from stockbrain.db.models.research import Thesis

__all__ = ["ApprovalAction", "ExecutionAttempt", "TradeProposal"]

#: Statuses in which a proposal still occupies its instrument.
ACTIVE_PROPOSAL_STATUSES: tuple[ProposalStatus, ...] = (
    ProposalStatus.DRAFT,
    ProposalStatus.READY,
    ProposalStatus.NOTIFIED,
    ProposalStatus.APPROVAL_PENDING,
    ProposalStatus.APPROVED,
    ProposalStatus.EXECUTING,
    ProposalStatus.EXECUTION_AMBIGUOUS,
)

_ACTIVE_STATUS_SQL = ", ".join(f"'{status.value}'" for status in ACTIVE_PROPOSAL_STATUSES)


class TradeProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trade_proposals"

    thesis_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    broker_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("broker_instruments.id", ondelete="SET NULL")
    )

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="default")
    broker_environment: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="demo")
    """Recorded at creation so a demo proposal can never be executed against live."""

    side: Mapped[OrderSide] = mapped_column(pg_enum(OrderSide, "order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(
        pg_enum(OrderType, "order_type"), nullable=False, default=OrderType.MARKET
    )
    proposed_quantity: Mapped[Decimal] = mapped_column(sa.Numeric(28, 10), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))

    reference_price: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    reference_currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    price_source: Mapped[PriceSource] = mapped_column(
        pg_enum(PriceSource, "price_source"), nullable=False
    )
    quote_timestamp: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    quote_age_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    estimated_notional: Mapped[Decimal] = mapped_column(sa.Numeric(24, 4), nullable=False)
    account_currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)

    risk_snapshot: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    risk_snapshot_hash: Mapped[str | None] = mapped_column(sa.String(64))

    status: Mapped[ProposalStatus] = mapped_column(
        pg_enum(ProposalStatus, "proposal_status"),
        nullable=False,
        default=ProposalStatus.DRAFT,
        server_default=ProposalStatus.DRAFT.value,
    )
    status_reason: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    notified_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(sa.Text)
    approved_channel: Mapped[ApprovalChannel | None] = mapped_column(
        pg_enum(ApprovalChannel, "approval_channel")
    )
    rejected_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(sa.Text)
    executed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")

    thesis: Mapped[Thesis | None] = relationship()
    approval_actions: Mapped[list[ApprovalAction]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    execution_attempts: Mapped[list[ExecutionAttempt]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )

    # ruff wants ClassVar here, but SQLAlchemy declares __mapper_args__ as an
    # instance variable on DeclarativeBase, so narrowing it fails type checking.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    __table_args__ = (
        sa.Index("ix_trade_proposals_status_expires", "status", "expires_at"),
        sa.Index("ix_trade_proposals_created_at", "created_at"),
        sa.Index(
            "uq_trade_proposals_active_instrument",
            "broker",
            "account_id",
            "broker_ticker",
            unique=True,
            postgresql_where=sa.text(f"status IN ({_ACTIVE_STATUS_SQL})"),
        ),
        sa.CheckConstraint("proposed_quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("reference_price > 0", name="reference_price_positive"),
        sa.CheckConstraint("quote_age_ms >= 0", name="quote_age_non_negative"),
    )

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        from stockbrain.db.base import utcnow

        return self.expires_at <= (now or utcnow())


class ApprovalAction(UUIDPrimaryKeyMixin, Base):
    """A single-use, opaque, server-side approval token.

    Telegram callback data and web request bodies carry only the opaque token.
    Order parameters (ticker, quantity, price, account) are *never* accepted
    from the client: they are re-read from the proposal row under lock.

    Only the SHA-256 of the token is stored, so a database leak does not yield
    usable approval tokens.
    """

    __tablename__ = "approval_actions"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[ApprovalChannel] = mapped_column(
        pg_enum(ApprovalChannel, "approval_channel"), nullable=False
    )
    stage: Mapped[ApprovalStage] = mapped_column(
        pg_enum(ApprovalStage, "approval_stage"), nullable=False
    )
    opaque_token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    user_identifier: Mapped[str] = mapped_column(sa.Text, nullable=False)
    """Telegram numeric user id, or the web account id.  Never a username."""

    parent_action_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("approval_actions.id", ondelete="SET NULL")
    )
    """A CONFIRM action must descend from the APPROVE action that produced it."""

    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    context: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))
    """Non-authoritative UI context (message id, chat id) for audit only."""

    proposal: Mapped[TradeProposal] = relationship(back_populates="approval_actions")

    __table_args__ = (
        sa.UniqueConstraint("opaque_token_hash", name="uq_approval_actions_opaque_token_hash"),
        sa.Index("ix_approval_actions_proposal_id", "proposal_id"),
    )


class ExecutionAttempt(UUIDPrimaryKeyMixin, Base):
    """One attempt to submit an order to the broker.

    ``sent_to_broker`` is set to ``True`` in the same transaction that precedes
    the HTTP request, *before* the response is known.  The partial unique index
    on that column therefore makes a second transmission for the same proposal
    impossible, which is exactly the guarantee a non-idempotent order endpoint
    requires.  A new attempt may only be created when every prior attempt has
    outcome ``FAILED_BEFORE_SEND``.
    """

    __tablename__ = "execution_attempts"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    started_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    broker_environment: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_payload: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    request_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """SHA-256 over (proposal, side, ticker, quantity, order type, environment)."""

    sent_to_broker: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    response_payload: Mapped[JSONDict | None] = mapped_column()
    broker_order_id: Mapped[str | None] = mapped_column(sa.Text)

    outcome: Mapped[ExecutionOutcome] = mapped_column(
        pg_enum(ExecutionOutcome, "execution_outcome"),
        nullable=False,
        default=ExecutionOutcome.PENDING,
        server_default=ExecutionOutcome.PENDING.value,
    )
    ambiguous: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    error: Mapped[str | None] = mapped_column(sa.Text)
    actor_identifier: Mapped[str | None] = mapped_column(sa.Text)
    rate_limit_headers: Mapped[JSONDict | None] = mapped_column()

    proposal: Mapped[TradeProposal] = relationship(back_populates="execution_attempts")

    __table_args__ = (
        sa.UniqueConstraint(
            "proposal_id", "attempt_number", name="uq_execution_attempts_proposal_id_attempt_number"
        ),
        sa.Index(
            "uq_execution_attempts_sent_once",
            "proposal_id",
            unique=True,
            postgresql_where=sa.text("sent_to_broker"),
        ),
        sa.Index("ix_execution_attempts_outcome", "outcome"),
        sa.CheckConstraint(
            "NOT sent_to_broker OR sent_at IS NOT NULL", name="sent_requires_timestamp"
        ),
    )
