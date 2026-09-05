"""The read model behind the bot's commands.

Handlers are adapters: they authenticate a numeric identity, parse a command,
call one method here, and format the answer.  Every database query lives in
this module so that no SQLAlchemy statement, risk calculation or broker call
appears in a chat handler, and so the same read model can back another client
later without being reimplemented.

Everything here is **read-only**.  The only state-changing operations the bot
performs go through :class:`~stockbrain.proposals.service.ProposalService` and
:class:`~stockbrain.control.state.ControlStateService`, which are the same
paths the web uses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.control.state import ControlSnapshot, ControlStateService
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import (
    EventStatus,
    ExecutionPolicy,
    ProposalStatus,
    ProviderStatus,
)
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.proposals.state_machine import ACTIVE_STATUSES, AUTHORIZABLE_STATUSES

__all__ = [
    "EventView",
    "PortfolioView",
    "PositionView",
    "ProposalView",
    "ResearchView",
    "StatusView",
    "TelegramService",
]

#: How many rows a list command shows.  A chat message that needs scrolling to
#: read is a message nobody reads.
LIST_LIMIT = 8


@dataclass(frozen=True, slots=True)
class StatusView:
    overall: ProviderStatus
    degraded_providers: list[tuple[str, ProviderStatus, str | None]]
    execution_policy: ExecutionPolicy
    proposals_enabled: bool
    broker_environment: str
    live_execution_permitted: bool
    automatic_authorization_permitted: bool
    control: ControlSnapshot
    proposal_counts: dict[str, int]
    awaiting_authorization: int
    telegram: dict[str, object]
    schema_current: bool


@dataclass(frozen=True, slots=True)
class PortfolioView:
    available: bool
    reason: str | None = None
    account_id: str | None = None
    currency: str | None = None
    total_value: Decimal | None = None
    invested_value: Decimal | None = None
    result_value: Decimal | None = None
    cash_available: Decimal | None = None
    cash_reserved: Decimal | None = None
    cash_in_pies: Decimal | None = None
    captured_at: dt.datetime | None = None
    broker_environment: str | None = None
    position_count: int = 0


@dataclass(frozen=True, slots=True)
class PositionView:
    broker_ticker: str
    name: str | None
    quantity: Decimal
    quantity_available: Decimal | None
    average_price: Decimal | None
    current_price: Decimal | None
    ppl: Decimal | None
    currency: str | None
    last_synced_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ProposalView:
    """Everything a proposal message renders.  No mutable domain object escapes."""

    id: uuid.UUID
    status: ProposalStatus
    status_reason: str | None
    execution_policy: ExecutionPolicy
    authorization_source: str | None
    approved_by: str | None
    company: str | None
    instrument_name: str | None
    broker: str
    broker_environment: str
    broker_ticker: str
    market_symbol: str | None
    side: str
    research_action: str | None
    quantity: Decimal
    notional: Decimal
    account_currency: str
    reference_price: Decimal
    reference_currency: str
    price_source: str
    quote_bid: Decimal | None
    quote_ask: Decimal | None
    quote_spread: Decimal | None
    quote_spread_bps: Decimal | None
    quote_age_ms: int
    market_session: str | None
    research_confidence: float | None
    thesis_summary: str | None
    risks: list[str]
    expires_at: dt.datetime
    created_at: dt.datetime
    blocking_reasons: list[str] = field(default_factory=list)

    #: Whether an execution attempt for this proposal has been *recorded as
    #: sent*.  Phase 7 hardcoded ``False`` because no order path existed; Phase 8
    #: reads it from ``execution_attempts.sent_to_broker``, which is written
    #: before the request rather than after the response -- so this says "bytes
    #: may have left", which is the fact that matters.
    broker_order_sent: bool = False
    broker_order_id: str | None = None
    execution_outcome: str | None = None
    execution_sent_at: dt.datetime | None = None
    execution_error_category: str | None = None
    reconciliation_result: str | None = None

    @property
    def execution_ambiguous(self) -> bool:
        return self.status is ProposalStatus.EXECUTION_AMBIGUOUS

    @property
    def awaiting_authorization(self) -> bool:
        return self.status in AUTHORIZABLE_STATUSES

    @property
    def is_manual(self) -> bool:
        return self.execution_policy is ExecutionPolicy.MANUAL


@dataclass(frozen=True, slots=True)
class EventView:
    id: uuid.UUID
    title: str
    status: EventStatus
    event_type: str | None
    first_seen_at: dt.datetime
    importance: float | None
    candidate_score: float | None
    companies: list[str]


@dataclass(frozen=True, slots=True)
class ResearchView:
    thesis_id: uuid.UUID
    company: str | None
    broker_ticker: str | None
    action: str
    confidence: float
    horizon: str
    summary: str | None
    bull_case: str | None
    bear_case: str | None
    catalysts: list[str]
    risks: list[str]
    invalidation_conditions: list[str]
    created_at: dt.datetime


class TelegramService:
    """Read-only domain queries for the bot's commands."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        health: ProviderHealthRegistry,
        control: ControlStateService,
    ) -> None:
        self._database = database
        self._settings = settings
        self._health = health
        self._control = control

    # ------------------------------------------------------------------
    async def status(self, *, telegram_status: dict[str, object] | None = None) -> StatusView:
        async with self._database.session() as session:
            counts = {
                str(row.status.value): int(row.tally)
                for row in (
                    await session.execute(
                        sa.select(TradeProposal.status, sa.func.count().label("tally")).group_by(
                            TradeProposal.status
                        )
                    )
                ).all()
            }
        awaiting = sum(counts.get(status.value, 0) for status in AUTHORIZABLE_STATUSES)
        degraded = [
            (state.provider.value, state.status, state.detail)
            for state in self._health.snapshot().values()
            if state.status in (ProviderStatus.DEGRADED, ProviderStatus.DOWN)
        ]
        schema_current, _ = self._health.schema_state
        return StatusView(
            overall=self._health.overall_status(),
            degraded_providers=sorted(degraded),
            execution_policy=self._settings.execution_policy,
            proposals_enabled=self._settings.proposals_enabled,
            broker_environment=self._settings.t212_env.value,
            live_execution_permitted=self._settings.live_execution_permitted,
            automatic_authorization_permitted=(self._settings.automatic_authorization_permitted),
            control=await self._control.snapshot(),
            proposal_counts=counts,
            awaiting_authorization=awaiting,
            telegram=telegram_status or {},
            schema_current=schema_current,
        )

    # ------------------------------------------------------------------
    async def portfolio(self) -> PortfolioView:
        """The most recent broker snapshot, or an honest reason there is none.

        Deliberately reports the *stored mirror* rather than issuing a broker
        request: ``/portfolio`` must not be a way for anyone in a chat to spend
        the account endpoint's one-request-per-five-seconds budget.
        """
        async with self._database.session() as session:
            snapshot = (
                (
                    await session.execute(
                        sa.select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            if snapshot is None:
                return PortfolioView(
                    available=False,
                    reason="no broker account snapshot has been captured yet",
                )
            positions = int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(Position)
                        .where(Position.account_id == (snapshot.account_id or "default"))
                    )
                ).scalar_one()
            )
        return PortfolioView(
            available=True,
            account_id=snapshot.account_id,
            currency=snapshot.currency,
            total_value=snapshot.total_value,
            invested_value=snapshot.invested_value,
            result_value=snapshot.result_value,
            cash_available=snapshot.cash_available,
            cash_reserved=snapshot.cash_reserved,
            cash_in_pies=snapshot.cash_in_pies,
            captured_at=snapshot.captured_at,
            broker_environment=snapshot.broker_environment,
            position_count=positions,
        )

    async def positions(self, limit: int = 20) -> list[PositionView]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sa.select(Position, BrokerInstrument.name)
                    .outerjoin(
                        BrokerInstrument,
                        sa.and_(
                            BrokerInstrument.broker_ticker == Position.broker_ticker,
                            BrokerInstrument.broker == Position.broker,
                        ),
                    )
                    .order_by(Position.broker_ticker)
                    .limit(limit)
                )
            ).all()
        return [
            PositionView(
                broker_ticker=position.broker_ticker,
                name=name,
                quantity=position.quantity,
                quantity_available=position.quantity_available,
                average_price=position.average_price,
                current_price=position.current_price,
                ppl=position.ppl,
                currency=position.currency,
                last_synced_at=position.last_synced_at,
            )
            for position, name in rows
        ]

    # ------------------------------------------------------------------
    async def proposals(self, *, limit: int = LIST_LIMIT) -> list[ProposalView]:
        """Active proposals first, then the most recent terminal ones."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    _proposal_query()
                    .order_by(
                        TradeProposal.status.in_(ACTIVE_STATUSES).desc(),
                        TradeProposal.created_at.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        return [_to_proposal_view(*row) for row in rows]

    async def proposal(self, proposal_id: uuid.UUID) -> ProposalView | None:
        async with self._database.session() as session:
            row = (
                await session.execute(_proposal_query().where(TradeProposal.id == proposal_id))
            ).first()
        return _to_proposal_view(*row) if row is not None else None

    # ------------------------------------------------------------------
    async def events(self, *, limit: int = LIST_LIMIT) -> list[EventView]:
        """Recent *significant* events: classified, candidate or researched.

        A raw ``NEW`` event has not been judged by anything yet, so surfacing it
        would be the "send every article to Telegram" the specification's
        notification policy rules out.
        """
        significant = (
            EventStatus.CLASSIFIED,
            EventStatus.CANDIDATE,
            EventStatus.RESEARCHING,
            EventStatus.RESEARCHED,
        )
        async with self._database.session() as session:
            events = list(
                (
                    await session.execute(
                        sa.select(Event)
                        .where(Event.status.in_(significant))
                        .order_by(Event.first_seen_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
            impacts: dict[uuid.UUID, list[str]] = {}
            if events:
                for event_id, name in (
                    await session.execute(
                        sa.select(
                            EventCompanyImpact.event_id,
                            EventCompanyImpact.company_name_hint,
                        )
                        .where(EventCompanyImpact.event_id.in_([e.id for e in events]))
                        .order_by(EventCompanyImpact.materiality_score.desc())
                    )
                ).all():
                    impacts.setdefault(event_id, []).append(name)
        return [
            EventView(
                id=event.id,
                title=event.title,
                status=event.status,
                event_type=event.event_type,
                first_seen_at=event.first_seen_at,
                importance=event.importance_score,
                candidate_score=event.candidate_score,
                companies=impacts.get(event.id, [])[:4],
            )
            for event in events
        ]

    # ------------------------------------------------------------------
    async def research(self, query: str) -> ResearchView | None:
        """Find the newest thesis for a proposal id, event id, or symbol.

        Only ever returns the *normalised* thesis: action, confidence, horizon,
        summary, bull and bear case, catalysts, risks and invalidation
        conditions.  Hidden model reasoning is not stored anywhere in StockBrain
        and therefore cannot be rendered here.
        """
        text = query.strip()
        if not text:
            return None
        async with self._database.session() as session:
            thesis_id = await self._locate_thesis(session, text)
            if thesis_id is None:
                return None
            row = (
                await session.execute(
                    sa.select(Thesis, TradeProposal, Company)
                    .outerjoin(TradeProposal, TradeProposal.thesis_id == Thesis.id)
                    .outerjoin(Company, Company.id == TradeProposal.company_id)
                    .where(Thesis.id == thesis_id)
                )
            ).first()
        if row is None:  # pragma: no cover - selected a statement ago
            return None
        thesis, proposal, company = row
        return ResearchView(
            thesis_id=thesis.id,
            company=company.name if company else None,
            broker_ticker=proposal.broker_ticker if proposal else None,
            action=thesis.action.value,
            confidence=thesis.confidence,
            horizon=thesis.time_horizon.value,
            summary=thesis.summary,
            bull_case=thesis.bull_case,
            bear_case=thesis.bear_case,
            catalysts=_items(thesis.catalysts),
            risks=_items(thesis.risks),
            invalidation_conditions=_items(thesis.invalidation_conditions),
            created_at=thesis.created_at,
        )

    async def _locate_thesis(self, session: AsyncSession, text: str) -> uuid.UUID | None:
        candidate_uuid: uuid.UUID | None = None
        try:
            candidate_uuid = uuid.UUID(text)
        except ValueError:
            candidate_uuid = None

        if candidate_uuid is not None:
            for statement in (
                sa.select(Thesis.id).where(Thesis.id == candidate_uuid),
                sa.select(TradeProposal.thesis_id).where(TradeProposal.id == candidate_uuid),
                sa.select(Thesis.id)
                .join(TradeProposal, TradeProposal.thesis_id == Thesis.id)
                .where(TradeProposal.event_id == candidate_uuid)
                .order_by(Thesis.created_at.desc()),
            ):
                found = await session.execute(statement)
                value = found.scalars().first()
                if value is not None:
                    return uuid.UUID(str(value))
            return None

        # A symbol. Matched against the proposal's broker ticker and against the
        # instrument's market symbol, case-insensitively -- never against a
        # company name, which is ambiguous by construction.
        symbol = text.upper()
        found = await session.execute(
            sa.select(Thesis.id)
            .join(TradeProposal, TradeProposal.thesis_id == Thesis.id)
            .outerjoin(BrokerInstrument, BrokerInstrument.id == TradeProposal.broker_instrument_id)
            .where(
                sa.or_(
                    sa.func.upper(TradeProposal.broker_ticker) == symbol,
                    sa.func.upper(BrokerInstrument.market_symbol) == symbol,
                )
            )
            .order_by(Thesis.created_at.desc())
        )
        value = found.scalars().first()
        return uuid.UUID(str(value)) if value is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _proposal_query() -> sa.Select[Any]:
    """Proposals with their listing, company, thesis and *transmitted* attempt.

    The attempt join is restricted to ``sent_to_broker`` because
    ``uq_execution_attempts_sent_once`` guarantees at most one such row per
    proposal -- so the join stays one-to-one and cannot duplicate a proposal.
    Refused attempts are numerous and are read on the detail endpoint instead.
    """
    return (
        sa.select(TradeProposal, BrokerInstrument, Company, Thesis, ExecutionAttempt)
        .outerjoin(BrokerInstrument, BrokerInstrument.id == TradeProposal.broker_instrument_id)
        .outerjoin(Company, Company.id == TradeProposal.company_id)
        .outerjoin(Thesis, Thesis.id == TradeProposal.thesis_id)
        .outerjoin(
            ExecutionAttempt,
            sa.and_(
                ExecutionAttempt.proposal_id == TradeProposal.id,
                ExecutionAttempt.sent_to_broker.is_(True),
            ),
        )
    )


def _to_proposal_view(
    proposal: TradeProposal,
    instrument: BrokerInstrument | None,
    company: Company | None,
    thesis: Thesis | None,
    attempt: ExecutionAttempt | None = None,
) -> ProposalView:
    return ProposalView(
        id=proposal.id,
        status=proposal.status,
        status_reason=proposal.status_reason,
        execution_policy=proposal.execution_policy,
        authorization_source=(
            proposal.authorization_source.value if proposal.authorization_source else None
        ),
        approved_by=proposal.approved_by,
        company=company.name if company else None,
        instrument_name=instrument.name if instrument else None,
        broker=proposal.broker.value,
        broker_environment=proposal.broker_environment,
        broker_ticker=proposal.broker_ticker,
        market_symbol=instrument.market_symbol if instrument else None,
        side=proposal.side.value,
        research_action=proposal.research_action,
        quantity=proposal.proposed_quantity,
        notional=proposal.estimated_notional,
        account_currency=proposal.account_currency,
        reference_price=proposal.reference_price,
        reference_currency=proposal.reference_currency,
        price_source=proposal.price_source.value,
        quote_bid=proposal.quote_bid,
        quote_ask=proposal.quote_ask,
        quote_spread=proposal.quote_spread,
        quote_spread_bps=proposal.quote_spread_bps,
        quote_age_ms=proposal.quote_age_ms,
        market_session=proposal.market_session,
        research_confidence=proposal.research_confidence,
        thesis_summary=thesis.summary if thesis else None,
        risks=_items(thesis.risks) if thesis else [],
        expires_at=proposal.expires_at,
        created_at=proposal.created_at,
        blocking_reasons=[
            str(rule.get("reason", ""))
            for rule in (proposal.risk_rules or [])
            if isinstance(rule, dict) and rule.get("outcome") == "BLOCK"
        ],
        broker_order_sent=attempt is not None and attempt.sent_to_broker,
        broker_order_id=attempt.broker_order_id if attempt else None,
        execution_outcome=attempt.outcome.value if attempt else None,
        execution_sent_at=attempt.sent_at if attempt else None,
        execution_error_category=attempt.error_category if attempt else None,
        reconciliation_result=attempt.reconciliation_result if attempt else None,
    )


def _items(value: object) -> list[str]:
    """Read the ``{"items": [...]}`` shape research persists, tolerantly."""
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return [str(item) for item in items if item]
    return []
