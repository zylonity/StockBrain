"""Trade proposals and the deterministic risk behind them.

These are the first state-changing routes in StockBrain, and the change they
make is entirely internal: a proposal moves between StockBrain's own states.
**No route here, and nothing they can reach, sends an order to a broker.**
Approving a proposal records that deterministic risk allowed the trade and who
signed it off; transmission arrives in Phase 8 behind its own gates.

Two rules shape every mutating endpoint:

* **Order parameters are never accepted from the client.**  The request body
  carries at most a free-text reason.  The ticker, side, quantity, price and
  account are re-read from the proposal row under lock, exactly as they will be
  when execution eventually reads them.  A client that could name a quantity
  would be a client that could size a trade.
* **The service does the deciding.**  These handlers translate exceptions into
  status codes and nothing else -- no route re-implements a risk check, and no
  route can skip one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from stockbrain.api.dependencies import DbSession, ServicesDep, SettingsDep
from stockbrain.broker.automation import automation_capability
from stockbrain.db.models.companies import BrokerInstrument, Company
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.enums import (
    AuthorizationSource,
    Broker,
    ExecutionPolicy,
    ProposalStatus,
    RiskOutcome,
)
from stockbrain.errors import (
    AuthorizationNotPermitted,
    ProposalAlreadyConsumed,
    ProposalExpired,
    ProposalInvalidated,
    RiskBlocked,
)
from stockbrain.proposals.service import ProposalService
from stockbrain.proposals.state_machine import ALLOWED_TRANSITIONS, AUTHORIZABLE_STATUSES
from stockbrain.risk.config import risk_config_from_settings

router = APIRouter(prefix="/api/v1", tags=["proposals"])

#: Until the authentication phase lands, the web UI is a single-operator LAN
#: surface and every web authorization is attributed to that operator. The
#: identifier is a server-side constant on purpose: an actor a client could
#: choose would be an audit trail a client could forge.
WEB_ACTOR = "web:local-operator"

AUTHORIZATION_NOTICE = (
    "Authorization means deterministic risk allowed this trade and a recorded authority "
    "signed it off. No broker order has been sent: StockBrain has no order-submission "
    "path in this phase."
)


class ActionRequest(BaseModel):
    """The complete mutable input a client may supply.

    Deliberately just a reason.  Nothing here can influence the side, the
    quantity, the price, the instrument or the account.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


class RuleView(BaseModel):
    rule_id: str
    rule_version: int
    outcome: str
    reason: str
    observed: str | None = None
    threshold: str | None = None
    max_notional: str | None = None
    size_factor: str | None = None


class ProposalView(BaseModel):
    id: uuid.UUID
    status: ProposalStatus
    status_reason: str | None

    thesis_id: uuid.UUID | None
    event_id: uuid.UUID | None
    company_id: uuid.UUID | None
    company_name: str | None
    research_run_id: uuid.UUID | None

    broker: str
    broker_ticker: str
    market_symbol: str | None
    instrument_name: str | None
    exchange: str | None
    instrument_currency: str | None
    isin: str | None
    account_id: str
    broker_environment: str

    side: str
    order_type: str
    proposed_quantity: Decimal
    max_quantity: Decimal | None
    estimated_notional: Decimal
    max_notional: Decimal | None
    reference_price: Decimal
    reference_currency: str
    account_currency: str

    price_source: str
    quote_provider: str | None
    quote_feed: str | None
    quote_bid: Decimal | None
    quote_ask: Decimal | None
    quote_mid: Decimal | None
    quote_spread: Decimal | None
    quote_spread_bps: Decimal | None
    quote_spread_status: str | None
    quote_timestamp: dt.datetime
    quote_age_ms: int
    market_session: str | None
    market_session_source: str | None

    research_action: str | None
    research_confidence: float | None
    thesis_summary: str | None
    time_horizon: str | None

    risk_outcome: RiskOutcome | None
    risk_policy_version: str | None
    risk_rules: list[RuleView]
    blockers: list[str]
    warnings: list[str]
    reductions: list[str]
    sizing_reasons: list[str]

    execution_policy: ExecutionPolicy
    authorization_source: AuthorizationSource | None
    approved_at: dt.datetime | None
    approved_by: str | None
    rejected_at: dt.datetime | None
    rejected_by: str | None
    invalidated_at: dt.datetime | None
    invalidation_reason: str | None

    created_at: dt.datetime
    expires_at: dt.datetime
    version: int

    can_approve: bool
    can_reject: bool
    can_cancel: bool
    broker_order_transmitted: bool = False
    notice: str = AUTHORIZATION_NOTICE


class ProposalListResponse(BaseModel):
    items: list[ProposalView]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[str, int]


class RiskDetailResponse(BaseModel):
    proposal_id: uuid.UUID
    risk_outcome: RiskOutcome | None
    risk_policy_version: str | None
    risk_snapshot_hash: str | None
    rules: list[RuleView]
    blockers: list[str]
    warnings: list[str]
    reductions: list[str]
    sizing_reasons: list[str]
    snapshot: dict[str, Any]
    evaluations: list[EvaluationView]


class EvaluationView(BaseModel):
    id: uuid.UUID
    stage: str
    outcome: RiskOutcome
    policy_version: str | None
    broker_ticker: str | None
    thesis_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    actor: str | None
    detail: str | None
    created_at: dt.datetime
    rules: list[RuleView]


class ExecutionPolicyResponse(BaseModel):
    execution_policy: ExecutionPolicy
    proposals_enabled: bool
    broker: str
    broker_environment: str
    automatic_authorization_permitted: bool
    automation_blockers: list[str]
    broker_automation: dict[str, Any]
    risk_policy_version: str
    risk_config: dict[str, Any]
    proposal_ttl_minutes: int
    broker_order_routes: list[str] = []
    notice: str = AUTHORIZATION_NOTICE


RiskDetailResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@router.get("/proposals/policy", response_model=ExecutionPolicyResponse)
async def proposal_policy(settings: SettingsDep) -> ExecutionPolicyResponse:
    """The execution policy, the broker's automation answer, and the limits.

    ``broker_order_routes`` is always empty and is returned rather than omitted:
    the absence of an order path is a property worth asserting on every poll,
    not one to infer from silence.
    """
    config = risk_config_from_settings(settings)
    return ExecutionPolicyResponse(
        execution_policy=settings.execution_policy,
        proposals_enabled=settings.proposals_enabled,
        broker=Broker.TRADING212.value,
        broker_environment=settings.t212_env.value,
        automatic_authorization_permitted=settings.automatic_authorization_permitted,
        automation_blockers=settings.automation_blockers,
        broker_automation=automation_capability(settings).as_dict(),
        risk_policy_version=config.version,
        risk_config=config.as_dict(),
        proposal_ttl_minutes=config.proposal_ttl_minutes,
    )


@router.get("/proposals", response_model=ProposalListResponse)
async def list_proposals(
    session: DbSession,
    proposal_status: Annotated[ProposalStatus | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProposalListResponse:
    filters = []
    if proposal_status is not None:
        filters.append(TradeProposal.status == proposal_status)
    if active_only:
        from stockbrain.proposals.state_machine import ACTIVE_STATUSES

        filters.append(TradeProposal.status.in_(ACTIVE_STATUSES))

    total = int(
        (
            await session.execute(
                sa.select(sa.func.count()).select_from(TradeProposal).where(*filters)
            )
        ).scalar_one()
    )
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
    rows = (
        await session.execute(
            _proposal_query()
            .where(*filters)
            .order_by(TradeProposal.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ProposalListResponse(
        items=[_to_view(*row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts_by_status=counts,
    )


@router.get("/proposals/{proposal_id}", response_model=ProposalView)
async def get_proposal(proposal_id: uuid.UUID, session: DbSession) -> ProposalView:
    row = (await session.execute(_proposal_query().where(TradeProposal.id == proposal_id))).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    return _to_view(*row)


@router.get("/proposals/{proposal_id}/risk", response_model=RiskDetailResponse)
async def get_proposal_risk(proposal_id: uuid.UUID, session: DbSession) -> RiskDetailResponse:
    """Every rule that ran, at generation and again at authorization."""
    proposal = await session.get(TradeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    evaluations = (
        await session.execute(
            sa.select(RiskEvaluation)
            .where(RiskEvaluation.proposal_id == proposal_id)
            .order_by(RiskEvaluation.created_at)
        )
    ).scalars()
    rules = _rules(proposal.risk_rules)
    return RiskDetailResponse(
        proposal_id=proposal.id,
        risk_outcome=proposal.risk_outcome,
        risk_policy_version=proposal.risk_policy_version,
        risk_snapshot_hash=proposal.risk_snapshot_hash,
        rules=rules,
        blockers=[rule.reason for rule in rules if rule.outcome == "BLOCK"],
        warnings=[rule.reason for rule in rules if rule.outcome == "WARN"],
        reductions=[rule.reason for rule in rules if rule.outcome == "REDUCE"],
        sizing_reasons=_reasons(proposal.sizing_reasons),
        snapshot=dict(proposal.risk_snapshot or {}),
        evaluations=[_to_evaluation(row) for row in evaluations],
    )


@router.get("/risk/evaluations", response_model=list[EvaluationView])
async def list_risk_evaluations(
    session: DbSession,
    thesis_id: Annotated[uuid.UUID | None, Query()] = None,
    outcome: Annotated[RiskOutcome | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EvaluationView]:
    """Risk decisions that produced no proposal are visible here.

    A refusal an operator cannot look at is indistinguishable from a bug, which
    is the same reason Phase 4 exposed its AMBIGUOUS resolutions.
    """
    query = sa.select(RiskEvaluation).order_by(RiskEvaluation.created_at.desc()).limit(limit)
    if thesis_id is not None:
        query = query.where(RiskEvaluation.thesis_id == thesis_id)
    if outcome is not None:
        query = query.where(RiskEvaluation.outcome == outcome)
    return [_to_evaluation(row) for row in await session.scalars(query)]


# ---------------------------------------------------------------------------
# Internal state changes.  Still no broker order anywhere.
# ---------------------------------------------------------------------------
@router.post("/proposals/{proposal_id}/approve", response_model=ProposalView)
async def approve_proposal(
    proposal_id: uuid.UUID,
    body: ActionRequest,
    session: DbSession,
    services: ServicesDep,
) -> ProposalView:
    """Authorize a proposal from the web.

    The service locks the row, re-reads the listing, re-reads broker account
    state, fetches a fresh execution-grade quote, re-runs every deterministic
    rule and only then records the authorization.  Anything that has become
    unsafe invalidates the proposal durably rather than being worked around.
    """
    proposals = _require_service(services)
    try:
        await proposals.authorize(
            proposal_id,
            source=AuthorizationSource.HUMAN_WEB,
            actor=WEB_ACTOR,
        )
    except ProposalExpired as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except (ProposalAlreadyConsumed, ProposalInvalidated) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AuthorizationNotPermitted as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RiskBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"deterministic risk refused this trade: {exc}",
        ) from exc
    return await _reload(session, proposal_id)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalView)
async def reject_proposal(
    proposal_id: uuid.UUID,
    body: ActionRequest,
    session: DbSession,
    services: ServicesDep,
) -> ProposalView:
    """Durably refuse a proposal.  Terminal; it is never revived."""
    proposals = _require_service(services)
    try:
        await proposals.reject(proposal_id, actor=WEB_ACTOR, reason=body.reason)
    except ProposalAlreadyConsumed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _reload(session, proposal_id)


@router.post("/proposals/{proposal_id}/cancel", response_model=ProposalView)
async def cancel_proposal(
    proposal_id: uuid.UUID,
    body: ActionRequest,
    session: DbSession,
    services: ServicesDep,
) -> ProposalView:
    """Withdraw a proposal, including one already authorized."""
    proposals = _require_service(services)
    try:
        await proposals.cancel(proposal_id, actor=WEB_ACTOR, reason=body.reason)
    except ProposalAlreadyConsumed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _reload(session, proposal_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_service(services: Any) -> ProposalService:
    proposals = getattr(services, "proposals", None) if services else None
    if not isinstance(proposals, ProposalService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the proposal service is not configured",
        )
    return proposals


def _proposal_query() -> sa.Select[Any]:
    return (
        sa.select(TradeProposal, BrokerInstrument, Company, Thesis)
        .outerjoin(BrokerInstrument, BrokerInstrument.id == TradeProposal.broker_instrument_id)
        .outerjoin(Company, Company.id == TradeProposal.company_id)
        .outerjoin(Thesis, Thesis.id == TradeProposal.thesis_id)
    )


async def _reload(session: DbSession, proposal_id: uuid.UUID) -> ProposalView:
    # The mutation ran in the service's own transaction; this session may hold a
    # snapshot from before it, so it is expired explicitly rather than trusted.
    session.expire_all()
    row = (await session.execute(_proposal_query().where(TradeProposal.id == proposal_id))).first()
    if row is None:  # pragma: no cover - the mutation just succeeded
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    return _to_view(*row)


def _rules(raw: list[dict[str, Any]] | None) -> list[RuleView]:
    """Rebuild stored rule results, skipping anything that no longer parses.

    Rules are a JSONB snapshot taken when the evaluation ran; a shape change
    must degrade the display, never 500 the endpoint that explains a refusal.
    """
    parsed: list[RuleView] = []
    for item in raw or []:
        try:
            parsed.append(RuleView.model_validate(item))
        except ValueError:
            continue
    return parsed


def _reasons(raw: list[dict[str, Any]] | None) -> list[str]:
    return [
        str(item["reason"]) for item in raw or [] if isinstance(item, dict) and "reason" in item
    ]


def _to_evaluation(row: RiskEvaluation) -> EvaluationView:
    return EvaluationView(
        id=row.id,
        stage=row.stage,
        outcome=row.outcome,
        policy_version=row.policy_version,
        broker_ticker=row.broker_ticker,
        thesis_id=row.thesis_id,
        proposal_id=row.proposal_id,
        actor=row.actor,
        detail=row.detail,
        created_at=row.created_at,
        rules=_rules(row.rules),
    )


def _to_view(
    proposal: TradeProposal,
    instrument: BrokerInstrument | None,
    company: Company | None,
    thesis: Thesis | None,
) -> ProposalView:
    rules = _rules(proposal.risk_rules)
    legal = ALLOWED_TRANSITIONS[proposal.status]
    return ProposalView(
        id=proposal.id,
        status=proposal.status,
        status_reason=proposal.status_reason,
        thesis_id=proposal.thesis_id,
        event_id=proposal.event_id,
        company_id=proposal.company_id,
        company_name=company.name if company else None,
        research_run_id=proposal.research_run_id,
        broker=proposal.broker.value,
        broker_ticker=proposal.broker_ticker,
        market_symbol=instrument.market_symbol if instrument else None,
        instrument_name=instrument.name if instrument else None,
        exchange=instrument.exchange if instrument else None,
        instrument_currency=instrument.currency if instrument else None,
        isin=instrument.isin if instrument else None,
        account_id=proposal.account_id,
        broker_environment=proposal.broker_environment,
        side=proposal.side.value,
        order_type=proposal.order_type.value,
        proposed_quantity=proposal.proposed_quantity,
        max_quantity=proposal.max_quantity,
        estimated_notional=proposal.estimated_notional,
        max_notional=proposal.max_notional,
        reference_price=proposal.reference_price,
        reference_currency=proposal.reference_currency,
        account_currency=proposal.account_currency,
        price_source=proposal.price_source.value,
        quote_provider=proposal.quote_provider,
        quote_feed=proposal.quote_feed,
        quote_bid=proposal.quote_bid,
        quote_ask=proposal.quote_ask,
        quote_mid=proposal.quote_mid,
        quote_spread=proposal.quote_spread,
        quote_spread_bps=proposal.quote_spread_bps,
        quote_spread_status=proposal.quote_spread_status,
        quote_timestamp=proposal.quote_timestamp,
        quote_age_ms=proposal.quote_age_ms,
        market_session=proposal.market_session,
        market_session_source=proposal.market_session_source,
        research_action=proposal.research_action,
        research_confidence=proposal.research_confidence,
        thesis_summary=thesis.summary if thesis else None,
        time_horizon=thesis.time_horizon.value if thesis else None,
        risk_outcome=proposal.risk_outcome,
        risk_policy_version=proposal.risk_policy_version,
        risk_rules=rules,
        blockers=[rule.reason for rule in rules if rule.outcome == "BLOCK"],
        warnings=[rule.reason for rule in rules if rule.outcome == "WARN"],
        reductions=[rule.reason for rule in rules if rule.outcome == "REDUCE"],
        sizing_reasons=_reasons(proposal.sizing_reasons),
        execution_policy=proposal.execution_policy,
        authorization_source=proposal.authorization_source,
        approved_at=proposal.approved_at,
        approved_by=proposal.approved_by,
        rejected_at=proposal.rejected_at,
        rejected_by=proposal.rejected_by,
        invalidated_at=proposal.invalidated_at,
        invalidation_reason=proposal.invalidation_reason,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
        version=proposal.version,
        can_approve=proposal.status in AUTHORIZABLE_STATUSES,
        can_reject=ProposalStatus.REJECTED in legal,
        can_cancel=ProposalStatus.CANCELLED in legal,
    )
