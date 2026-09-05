"""Execution status, attempt detail, and the one safe control.

Read-only apart from a single mutating route, and that route is deliberately
**reconciliation**, not retry:

* ``GET  /api/v1/execution/status``               deployment posture and counts
* ``GET  /api/v1/proposals/{id}/execution``       one proposal's whole journey
* ``GET  /api/v1/execution/attempts``             recent attempts
* ``GET  /api/v1/execution/orders``               the local broker-order mirror
* ``POST /api/v1/execution/attempts/{id}/reconcile``  read the broker again

**There is no resend endpoint, and there must not be one.**  Trading 212
documents the order POST as non-idempotent, so a "retry" button on an ambiguous
attempt is a button that creates a second real position.  The only thing an
operator can ask for here is another *read*, which is safe to repeat because a
GET has no side effect.

A proposal whose order was provably never transmitted needs no endpoint either:
its attempt is ``FAILED_BEFORE_SEND``, the proposal is back at ``APPROVED``, and
the ordinary execution sweep picks it up again.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from stockbrain.api.dependencies import DatabaseDep, DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    BrokerOrderResponse,
    ExecutionAttemptResponse,
    ExecutionStatusSummaryResponse,
    ProposalExecutionResponse,
    ReconciliationTriggerRequest,
)
from stockbrain.broker.trading212_orders import MARKET_ORDER_PATH
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.enums import Broker, ExecutionOutcome, ProposalStatus
from stockbrain.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["execution"])

log = get_logger(__name__)

_AMBIGUOUS_NOTICE = (
    "ORDER STATE UNKNOWN. StockBrain transmitted a request and did not receive a "
    "definitive response, so the order may or may not exist at the broker. It will "
    "NOT be retried automatically and must NOT be resent manually. Reconciliation is "
    "reading Trading 212's pending orders and order history; until it resolves, this "
    "proposal continues to reserve its exposure."
)

_NORMAL_NOTICE = (
    "Every order is transmitted at most once. `sent_to_broker` is recorded before the "
    "HTTP request rather than after the response, so it means 'bytes may have left' -- "
    "which is the fact that matters when a response never arrives."
)


@router.get(
    "/execution/status",
    response_model=ExecutionStatusSummaryResponse,
    summary="Broker execution posture and attempt counts",
)
async def execution_status(
    session: DbSession, settings: SettingsDep, database: DatabaseDep
) -> ExecutionStatusSummaryResponse:
    counts = {
        str(row.outcome.value): int(row.tally)
        for row in (
            await session.execute(
                sa.select(ExecutionAttempt.outcome, sa.func.count().label("tally")).group_by(
                    ExecutionAttempt.outcome
                )
            )
        ).all()
    }
    pending = int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(ExecutionAttempt)
                .where(
                    ExecutionAttempt.sent_to_broker.is_(True),
                    ExecutionAttempt.outcome.in_(
                        (ExecutionOutcome.PENDING, ExecutionOutcome.AMBIGUOUS)
                    ),
                )
            )
        ).scalar_one()
    )
    # The durable pause/kill state, read from PostgreSQL rather than from a
    # process flag, so this response matches what a send-time check will see.
    control = await ControlStateService(database).snapshot()
    return ExecutionStatusSummaryResponse(
        broker=Broker.TRADING212.value,
        broker_environment=settings.t212_env.value,
        order_transmission_permitted=settings.order_transmission_permitted,
        blockers=settings.order_transmission_blockers,
        execution_mode=settings.execution_mode.value,
        execution_policy=settings.execution_policy.value,
        live_execution_permitted=settings.live_execution_permitted,
        automated_trading_consent_confirmed=(settings.t212_automated_trading_consent_confirmed),
        trading_halted=control.trading_halted,
        control_blockers=control.blockers,
        attempts_by_outcome=counts,
        ambiguous_attempts=counts.get(ExecutionOutcome.AMBIGUOUS.value, 0),
        reconciliation_pending=pending,
        order_endpoint=MARKET_ORDER_PATH,
        # Reported rather than assumed. Trading 212 states the endpoint is not
        # idempotent, and a client that forgot would build a retry.
        order_endpoint_idempotent=False,
        notice=_NORMAL_NOTICE,
    )


@router.get(
    "/proposals/{proposal_id}/execution",
    response_model=ProposalExecutionResponse,
    summary="One proposal's execution attempts and broker orders",
)
async def proposal_execution(
    proposal_id: uuid.UUID, session: DbSession
) -> ProposalExecutionResponse:
    proposal = await session.get(TradeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

    attempts = list(
        (
            await session.execute(
                sa.select(ExecutionAttempt)
                .where(ExecutionAttempt.proposal_id == proposal_id)
                .order_by(ExecutionAttempt.attempt_number)
            )
        ).scalars()
    )
    orders = list(
        (
            await session.execute(
                sa.select(BrokerOrder)
                .where(BrokerOrder.proposal_id == proposal_id)
                .order_by(BrokerOrder.submitted_at)
            )
        ).scalars()
    )
    transmitted = any(attempt.sent_to_broker for attempt in attempts)
    ambiguous = proposal.status is ProposalStatus.EXECUTION_AMBIGUOUS or any(
        attempt.ambiguous for attempt in attempts
    )
    return ProposalExecutionResponse(
        proposal_id=proposal_id,
        proposal_status=proposal.status,
        broker_environment=proposal.broker_environment,
        authorization_source=(
            proposal.authorization_source.value if proposal.authorization_source else None
        ),
        execution_policy=proposal.execution_policy.value,
        transmitted=transmitted,
        ambiguous=ambiguous,
        reconciliation_required=ambiguous,
        attempts=[_attempt_view(attempt) for attempt in attempts],
        orders=[_order_view(order) for order in orders],
        notice=_AMBIGUOUS_NOTICE if ambiguous else _NORMAL_NOTICE,
    )


@router.get(
    "/execution/attempts",
    response_model=list[ExecutionAttemptResponse],
    summary="Recent execution attempts",
)
async def list_attempts(
    session: DbSession,
    ambiguous_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ExecutionAttemptResponse]:
    query = sa.select(ExecutionAttempt).order_by(ExecutionAttempt.started_at.desc()).limit(limit)
    if ambiguous_only:
        query = query.where(ExecutionAttempt.ambiguous.is_(True))
    rows = list((await session.execute(query)).scalars())
    return [_attempt_view(attempt) for attempt in rows]


@router.get(
    "/execution/orders",
    response_model=list[BrokerOrderResponse],
    summary="StockBrain's mirror of broker orders",
)
async def list_orders(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BrokerOrderResponse]:
    rows = list(
        (
            await session.execute(
                sa.select(BrokerOrder).order_by(BrokerOrder.last_synced_at.desc()).limit(limit)
            )
        ).scalars()
    )
    return [_order_view(order) for order in rows]


@router.post(
    "/execution/attempts/{attempt_id}/reconcile",
    response_model=ExecutionAttemptResponse,
    summary="Ask the broker again what happened to an attempt",
)
async def reconcile_attempt(
    attempt_id: uuid.UUID,
    body: ReconciliationTriggerRequest,
    session: DbSession,
    services: ServicesDep,
) -> ExecutionAttemptResponse:
    """Re-read the broker for one attempt.  Transmits nothing.

    Safe to invoke repeatedly because it is a *read*: it fetches pending orders
    and order history and compares them against the attempt's recorded facts.
    The one thing it cannot do -- by construction, since no code path exists --
    is send another order.
    """
    reconciliation = getattr(services, "reconciliation", None) if services else None
    if reconciliation is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the reconciliation service is not configured",
        )
    attempt = await session.get(ExecutionAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attempt not found")

    outcome = await reconciliation.reconcile(attempt_id)
    log.info(
        "manual_reconciliation_requested",
        attempt_id=str(attempt_id),
        result=outcome.result.value,
        order_resent=False,
    )
    session.expire_all()
    refreshed = await session.get(ExecutionAttempt, attempt_id)
    if refreshed is None:  # pragma: no cover - just read it
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attempt not found")
    return _attempt_view(refreshed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _attempt_view(attempt: ExecutionAttempt) -> ExecutionAttemptResponse:
    return ExecutionAttemptResponse(
        id=attempt.id,
        proposal_id=attempt.proposal_id,
        attempt_number=attempt.attempt_number,
        broker_environment=attempt.broker_environment,
        outcome=attempt.outcome.value,
        ambiguous=attempt.ambiguous,
        sent_to_broker=attempt.sent_to_broker,
        sent_at=attempt.sent_at,
        started_at=attempt.started_at,
        preflight_at=attempt.preflight_at,
        completed_at=attempt.completed_at,
        http_status=attempt.http_status,
        broker_order_id=attempt.broker_order_id,
        request_fingerprint=attempt.request_fingerprint,
        error=attempt.error,
        error_category=attempt.error_category,
        reconciled_at=attempt.reconciled_at,
        reconciliation_result=attempt.reconciliation_result,
        reconciliation_attempts=attempt.reconciliation_attempts,
        reconciliation_detail=dict(attempt.reconciliation_detail or {}),
        rate_limit=dict(attempt.rate_limit_headers or {}),
        execution_snapshot=dict(attempt.execution_snapshot or {}),
        # An attempt that was recorded as sent may never be resent, whatever its
        # outcome. A pre-send failure needs no flag: the sweep re-drives it.
        resend_permitted=False,
    )


def _order_view(order: BrokerOrder) -> BrokerOrderResponse:
    return BrokerOrderResponse(
        broker=order.broker.value,
        broker_order_id=order.broker_order_id,
        broker_environment=order.broker_environment,
        broker_ticker=order.broker_ticker,
        side=order.side.value,
        order_type=order.order_type.value,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        filled_value=order.filled_value,
        currency=order.currency,
        broker_status=order.broker_status,
        initiated_from=order.initiated_from,
        is_terminal=order.is_terminal,
        discovered_by_reconciliation=order.discovered_by_reconciliation,
        submitted_at=order.submitted_at,
        last_synced_at=order.last_synced_at,
    )
