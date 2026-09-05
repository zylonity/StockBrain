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
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import (
    ApprovalChannel,
    ApprovalStage,
    AuthorizationSource,
    Broker,
    ExecutionOutcome,
    ExecutionPolicy,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    RiskOutcome,
)
from stockbrain.proposals.state_machine import ACTIVE_STATUSES

if TYPE_CHECKING:
    from stockbrain.db.models.research import Thesis

__all__ = ["ApprovalAction", "ExecutionAttempt", "RiskEvaluation", "TradeProposal"]

#: Statuses in which a proposal still occupies its instrument.  Defined by the
#: state machine so the index predicate and the transition table cannot drift.
ACTIVE_PROPOSAL_STATUSES: tuple[ProposalStatus, ...] = ACTIVE_STATUSES

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

    # ------------------------------------------------------------------
    # Phase 6: the exact values the decision was made on
    # ------------------------------------------------------------------
    risk_outcome: Mapped[RiskOutcome | None] = mapped_column(pg_enum(RiskOutcome, "risk_outcome"))
    risk_policy_version: Mapped[str | None] = mapped_column(sa.String(64))
    """Content hash of every threshold in force when this was evaluated. A
    proposal generated under superseded limits is invalidated rather than
    quietly authorized against numbers nobody chose."""

    risk_rules: Mapped[list[JSONDict]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    """One structured entry per rule: id, version, outcome, observed value,
    threshold and reason. A refusal recorded as free text is one nobody can
    audit or aggregate."""

    quote_provider: Mapped[str | None] = mapped_column(sa.Text)
    quote_feed: Mapped[str | None] = mapped_column(sa.Text)
    quote_bid: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    quote_ask: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    quote_mid: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    quote_spread: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    quote_spread_bps: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 4))
    quote_spread_status: Mapped[str | None] = mapped_column(sa.Text)
    market_session: Mapped[str | None] = mapped_column(sa.Text)
    market_session_source: Mapped[str | None] = mapped_column(sa.Text)

    research_confidence: Mapped[float | None] = mapped_column(sa.Float)
    research_action: Mapped[str | None] = mapped_column(sa.Text)
    research_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="SET NULL")
    )

    execution_policy: Mapped[ExecutionPolicy] = mapped_column(
        pg_enum(ExecutionPolicy, "execution_policy"),
        nullable=False,
        default=ExecutionPolicy.MANUAL,
        server_default=ExecutionPolicy.MANUAL.value,
    )
    """Recorded at creation. Flipping the deployment's policy therefore cannot
    retroactively authorize proposals generated under the other one."""

    authorization_source: Mapped[AuthorizationSource | None] = mapped_column(
        pg_enum(AuthorizationSource, "authorization_source")
    )
    authorization_policy_snapshot: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    """The execution policy and broker automation capability as they stood at
    authorization, so a later configuration change cannot rewrite the record of
    what was permitted at the time."""

    dedupe_key: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    """One proposal per thesis/listing/policy. At-least-once job delivery means
    the generator will eventually run twice; this makes the second run a no-op
    at the database rather than at a hopeful check."""

    invalidated_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(sa.Text)
    sizing_reasons: Mapped[list[JSONDict]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    max_quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(28, 10))
    max_notional: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 4))

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
        sa.Index("ix_trade_proposals_thesis_id", "thesis_id"),
        sa.Index(
            "uq_trade_proposals_active_instrument",
            "broker",
            "account_id",
            "broker_ticker",
            unique=True,
            postgresql_where=sa.text(f"status IN ({_ACTIVE_STATUS_SQL})"),
        ),
        # One live proposal per thesis. `dedupe_key` already makes a redelivered
        # generation job a no-op, but it is scoped to the risk-policy version;
        # this covers the case where the policy changed between two attempts on
        # the same conclusion, which should still produce one proposal.
        sa.Index(
            "uq_trade_proposals_active_thesis",
            "thesis_id",
            unique=True,
            postgresql_where=sa.text(f"thesis_id IS NOT NULL AND status IN ({_ACTIVE_STATUS_SQL})"),
        ),
        sa.CheckConstraint("proposed_quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("reference_price > 0", name="reference_price_positive"),
        sa.CheckConstraint("quote_age_ms >= 0", name="quote_age_non_negative"),
        # An APPROVED proposal must say what authorized it and when. Without
        # this, a bug that set the status without the provenance would produce
        # an authorized trade nobody can attribute -- and attribution is the
        # entire point of recording an authorization.
        sa.CheckConstraint(
            "status <> 'APPROVED' "
            "OR (authorization_source IS NOT NULL AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL)",
            name="approved_requires_authorization_provenance",
        ),
        # A system authorization is only legal on a proposal that was generated
        # under the AUTOMATIC policy. This is what makes "changing the policy
        # cannot retroactively authorize existing proposals" a database
        # guarantee rather than a service-layer intention.
        sa.CheckConstraint(
            "authorization_source <> 'SYSTEM_AUTOMATIC' OR execution_policy = 'AUTOMATIC'",
            name="system_auth_requires_automatic_policy",
        ),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR status = 'INVALIDATED'",
            name="invalidated_requires_status",
        ),
    )

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        from stockbrain.db.base import utcnow

        return self.expires_at <= (now or utcnow())


class RiskEvaluation(UUIDPrimaryKeyMixin, Base):
    """One run of the deterministic risk engine, allowed or blocked.

    Blocked evaluations are the reason this table exists.  A refusal that leaves
    no row is a refusal an operator can only find in logs, and "why did nothing
    get proposed for this thesis?" is exactly the question a risk engine must be
    able to answer about itself.  A proposal row cannot serve the purpose: its
    quantity, side and reference price are ``NOT NULL`` and positive, and a
    blocked evaluation has none of them -- inventing values to satisfy the schema
    would be recording a trade that was never contemplated.

    Rows are also written at *authorization* time, so the record shows what was
    true when the trade was signed off, not only when it was drafted.
    """

    __tablename__ = "risk_evaluations"

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    stage: Mapped[str] = mapped_column(sa.Text, nullable=False)
    """GENERATION | AUTHORIZATION | REVALIDATION."""

    thesis_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="SET NULL")
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="SET NULL")
    )
    broker_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("broker_instruments.id", ondelete="SET NULL")
    )
    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_ticker: Mapped[str | None] = mapped_column(sa.Text)

    outcome: Mapped[RiskOutcome] = mapped_column(
        pg_enum(RiskOutcome, "risk_outcome"), nullable=False
    )
    policy_version: Mapped[str | None] = mapped_column(sa.String(64))
    rules: Mapped[list[JSONDict]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    snapshot: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    snapshot_hash: Mapped[str | None] = mapped_column(sa.String(64))
    actor: Mapped[str | None] = mapped_column(sa.Text)
    detail: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_risk_evaluations_thesis_id", "thesis_id"),
        sa.Index("ix_risk_evaluations_proposal_id", "proposal_id"),
        sa.Index("ix_risk_evaluations_created_at", "created_at"),
    )


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
