"""Resolving an unknown outcome by reading, never by sending.

Reconciliation exists because a non-idempotent order endpoint plus an unreliable
network produces a state no amount of care avoids: *the order may or may not
exist*.  There are two honest ways out -- find it, or prove its absence -- and a
third answer that matters just as much: **inconclusive**.

Guessing costs money in both directions.  Concluding "not placed" when an order
exists releases the reservation and lets the next proposal be sized against
money that is already committed.  Concluding "placed" when none exists strands
an authorized trade forever.  So absence is only evidence when both broker read
paths answered *and* the broker has had time to make a new order visible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    Broker,
    ExecutionOutcome,
    OrderSide,
    OrderType,
    ProposalStatus,
    ReconciliationResult,
)
from stockbrain.errors import AmbiguousTransportFailure, ProviderUnavailable
from stockbrain.execution.models import CandidateSearch
from stockbrain.execution.reconciliation import ReconciliationService
from stockbrain.execution.service import ExecutionService
from tests import proposal_helpers as helpers
from tests.execution_helpers import FakeProvider, order_view

pytestmark = pytest.mark.integration

TICKER = "AAPL_US_EQ"


def settings_for(**overrides: object) -> Settings:
    base: dict[str, object] = {"t212_execution_enabled": True}
    base.update(overrides)
    return helpers.settings(**base)


async def ambiguous_attempt(
    database: Database, settings: Settings, provider: FakeProvider
) -> tuple[uuid.UUID, uuid.UUID]:
    """Drive a real proposal to a real ambiguous attempt.

    Built through the execution service rather than by inserting rows, so the
    attempt carries the same request payload and snapshot reconciliation will
    actually read.
    """
    control = ControlStateService(database)
    proposals = helpers.service_with(database, settings, control=control)
    await helpers.seed(database)
    await helpers.fund(database, environment=settings.t212_env.value)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None, generated.reason
    await proposals.authorize(
        generated.proposal_id,
        source=AuthorizationSource.HUMAN_WEB,
        actor="web:local-operator",
    )
    provider.error = AmbiguousTransportFailure("read timed out")
    execution = ExecutionService(
        database,
        settings,
        proposals=proposals,
        provider=provider,
        control=control,
        broker=Broker.TRADING212,
    )
    result = await execution.execute(generated.proposal_id)
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert result.attempt_id is not None
    provider.error = None
    return generated.proposal_id, result.attempt_id


def reconciler(
    database: Database, settings: Settings, provider: FakeProvider
) -> ReconciliationService:
    control = ControlStateService(database)
    return ReconciliationService(
        database,
        settings,
        provider=provider,
        proposals=helpers.service_with(database, settings, control=control),
        broker=Broker.TRADING212,
    )


def _after_settle(settings: Settings) -> dt.datetime:
    """A moment past the settle window, for tests about *matching*.

    The window's own behaviour has its own test; these want to reach the
    matching verdict without waiting for it.
    """
    return utcnow() + dt.timedelta(seconds=settings.execution_reconcile_min_age_seconds + 5)


async def attempt_of(database: Database, attempt_id: uuid.UUID) -> ExecutionAttempt:
    async with database.session() as session:
        attempt = await session.get(ExecutionAttempt, attempt_id)
    assert attempt is not None
    return attempt


# ---------------------------------------------------------------------------
# One exact match
# ---------------------------------------------------------------------------
async def test_one_matching_api_order_resolves_the_attempt(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("2"), status="NEW"),),
        pending_ok=True,
        history_ok=True,
        scanned=1,
    )

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.ORDER_FOUND
    assert outcome.broker_order_id == "500100"
    assert provider.submitted == 1, "reconciliation transmits nothing"

    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.SUBMITTED
    assert attempt.ambiguous is False
    assert attempt.broker_order_id == "500100"
    assert attempt.reconciled_at is not None
    assert attempt.reconciliation_attempts == 1

    async with clean_tables.session() as session:
        order = (
            await session.execute(
                sa.select(BrokerOrder).where(BrokerOrder.proposal_id == proposal_id)
            )
        ).scalar_one()
    assert order.discovered_by_reconciliation is True
    assert order.broker_environment == "demo"


async def test_a_filled_match_marks_the_proposal_executed(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(
            order_view(
                signed_quantity=Decimal("2"),
                status="FILLED",
                filled_quantity=Decimal("2"),
                filled_value=Decimal("400.10"),
            ),
        ),
        pending_ok=True,
        history_ok=True,
    )

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.ORDER_FOUND
    assert outcome.proposal_status is ProposalStatus.EXECUTED
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.RECONCILED_FILLED
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None and proposal.executed_at is not None


async def test_a_rejected_match_fails_the_proposal(clean_tables: Database) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("2"), status="REJECTED"),),
        pending_ok=True,
        history_ok=True,
    )

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)
    assert outcome.result is ReconciliationResult.ORDER_FOUND
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None and proposal.status is ProposalStatus.FAILED


# ---------------------------------------------------------------------------
# Matching is deliberately narrow
# ---------------------------------------------------------------------------
async def test_an_order_the_operator_placed_in_the_app_is_never_matched(
    clean_tables: Database,
) -> None:
    """``initiatedFrom`` is the strongest evidence this API offers.

    Without it, a buy the operator made on their phone for the same stock in the
    same minute would be attributed to StockBrain's ambiguous attempt -- and the
    real order would stay unaccounted for.
    """
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(
            order_view(signed_quantity=Decimal("2"), initiated_from="IOS"),
            order_view(broker_order_id="9", signed_quantity=Decimal("2"), initiated_from="WEB"),
        ),
        pending_ok=True,
        history_ok=True,
        scanned=2,
    )

    outcome = await reconciler(clean_tables, settings, provider).reconcile(
        attempt_id, now=_after_settle(settings)
    )
    assert outcome.result is ReconciliationResult.ORDER_NOT_PLACED
    assert outcome.candidates == 0


async def test_a_buy_never_matches_a_sell_of_the_same_size(
    clean_tables: Database,
) -> None:
    """The signed quantity is matched, not its magnitude."""
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("-2"), side="SELL"),),
        pending_ok=True,
        history_ok=True,
        scanned=1,
    )
    outcome = await reconciler(clean_tables, settings, provider).reconcile(
        attempt_id, now=_after_settle(settings)
    )
    assert outcome.result is ReconciliationResult.ORDER_NOT_PLACED


async def test_a_different_quantity_or_ticker_or_type_never_matches(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(
            order_view(broker_order_id="1", signed_quantity=Decimal("3")),
            order_view(broker_order_id="2", broker_ticker="MSFT_US_EQ"),
            order_view(broker_order_id="3", order_type="LIMIT"),
            order_view(broker_order_id="4", signed_quantity=None),
        ),
        pending_ok=True,
        history_ok=True,
        scanned=4,
    )
    outcome = await reconciler(clean_tables, settings, provider).reconcile(
        attempt_id, now=_after_settle(settings)
    )
    assert outcome.result is ReconciliationResult.ORDER_NOT_PLACED


async def test_multiple_plausible_matches_stay_ambiguous(
    clean_tables: Database,
) -> None:
    """Genuinely indistinguishable through this API.

    Trading 212 accepts no client-supplied reference, so two identical
    API-initiated market orders on the same listing in the same window cannot be
    told apart. The limitation is reported, not resolved by picking one.
    """
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(
            order_view(broker_order_id="10", signed_quantity=Decimal("2")),
            order_view(broker_order_id="11", signed_quantity=Decimal("2")),
        ),
        pending_ok=True,
        history_ok=True,
        scanned=2,
    )

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.MULTIPLE_CANDIDATES
    assert outcome.candidates == 2
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS
    assert attempt.ambiguous is True
    assert "no client reference" in attempt.reconciliation_detail["detail"]
    # The proposal keeps reserving its exposure while the outcome is unknown.
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.status is ProposalStatus.EXECUTION_AMBIGUOUS


async def test_an_order_already_mirrored_elsewhere_is_excluded(
    clean_tables: Database,
) -> None:
    """Two attempts must not both claim the same broker order.

    An order already in the mirror -- whether attributed to another attempt or
    to none at all -- is not available to be claimed by this one, or each would
    report success for the same single order.
    """
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)

    # Already in the mirror, attributed to nothing this attempt owns.
    async with clean_tables.transaction() as session:
        session.add(
            BrokerOrder(
                broker=Broker.TRADING212,
                broker_order_id="500100",
                broker_ticker=TICKER,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("2"),
            )
        )
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("2")),),
        pending_ok=True,
        history_ok=True,
        scanned=1,
    )
    outcome = await reconciler(clean_tables, settings, provider).reconcile(
        attempt_id, now=_after_settle(settings)
    )
    assert outcome.result is ReconciliationResult.ORDER_NOT_PLACED
    assert proposal_id is not None


# ---------------------------------------------------------------------------
# Absence is only evidence under two conditions
# ---------------------------------------------------------------------------
async def test_absence_is_not_evidence_when_a_read_path_failed(
    clean_tables: Database,
) -> None:
    """ "We looked and found nothing" and "we could not look" are different."""
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(pending_ok=True, history_ok=False, scanned=0)

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.BROKER_UNAVAILABLE
    assert "absence is not evidence" in outcome.detail
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS


async def test_absence_is_not_evidence_inside_the_settle_window(
    clean_tables: Database,
) -> None:
    """A new order may not be visible yet.

    Concluding absence too early releases the reservation for an order that
    exists, so a negative conclusion has to outlive the broker's own propagation
    delay.
    """
    settings = settings_for(execution_reconcile_min_age_seconds=600)
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(pending_ok=True, history_ok=True, scanned=0)

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.INCONCLUSIVE
    assert "settle window" in outcome.detail
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS


async def test_proven_absence_after_the_settle_window_fails_the_proposal(
    clean_tables: Database,
) -> None:
    """The only path that releases a reservation for an attempt recorded as sent."""
    settings = settings_for(execution_reconcile_min_age_seconds=5)
    provider = FakeProvider(_environment="demo")
    proposal_id, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(pending_ok=True, history_ok=True, scanned=7)

    outcome = await reconciler(clean_tables, settings, provider).reconcile(
        attempt_id, now=utcnow() + dt.timedelta(seconds=30)
    )

    assert outcome.result is ReconciliationResult.ORDER_NOT_PLACED
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.RECONCILED_NOT_PLACED
    assert attempt.ambiguous is False
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None and proposal.status is ProposalStatus.FAILED


async def test_a_broker_read_failure_leaves_the_attempt_ambiguous(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.fetch_error = ProviderUnavailable("trading212 is down")

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert outcome.result is ReconciliationResult.BROKER_UNAVAILABLE
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS
    assert attempt.reconciliation_attempts == 1


# ---------------------------------------------------------------------------
# A known order id
# ---------------------------------------------------------------------------
async def test_a_known_order_id_is_looked_up_directly(clean_tables: Database) -> None:
    """The easy case: ask about the id we were given."""
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    control = ControlStateService(clean_tables)
    proposals = helpers.service_with(clean_tables, settings, control=control)
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None
    await proposals.authorize(
        generated.proposal_id,
        source=AuthorizationSource.HUMAN_WEB,
        actor="web:local-operator",
    )
    execution = ExecutionService(
        clean_tables,
        settings,
        proposals=proposals,
        provider=provider,
        control=control,
    )
    result = await execution.execute(generated.proposal_id)
    assert result.attempt_id is not None

    provider.orders = {"500100": order_view(status="FILLED", filled_quantity=Decimal("2"))}
    outcome = await reconciler(clean_tables, settings, provider).reconcile(result.attempt_id)
    assert outcome.result is ReconciliationResult.ORDER_FOUND
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, generated.proposal_id)
    assert proposal is not None and proposal.status is ProposalStatus.EXECUTED


async def test_a_known_id_the_broker_denies_stays_inconclusive(
    clean_tables: Database,
) -> None:
    """Not concluding absence from a single read.

    A pending-order lookup 404s the moment an order fills, so "no such order"
    from one endpoint is not proof that none exists.
    """
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    async with clean_tables.transaction() as session:
        attempt = await session.get(ExecutionAttempt, attempt_id)
        assert attempt is not None
        attempt.broker_order_id = "777"
    provider.orders = {}
    provider.search = CandidateSearch(pending_ok=True, history_ok=True)

    outcome = await reconciler(clean_tables, settings, provider).reconcile(attempt_id)
    assert outcome.result is ReconciliationResult.INCONCLUSIVE
    assert "no such order" in outcome.detail


# ---------------------------------------------------------------------------
# Environment and sweeping
# ---------------------------------------------------------------------------
async def test_a_worker_in_the_other_environment_does_not_reconcile(
    clean_tables: Database,
) -> None:
    """A live-configured worker must not read -- or write -- demo order state."""
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)

    live_provider = FakeProvider(_environment="live")
    outcome = await reconciler(clean_tables, settings, live_provider).reconcile(attempt_id)
    assert outcome.result is ReconciliationResult.INCONCLUSIVE
    assert "configured for 'live'" in outcome.detail
    assert live_provider.searches == 0


async def test_the_sweep_reconciles_a_bounded_batch(clean_tables: Database) -> None:
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("2")),),
        pending_ok=True,
        history_ok=True,
    )

    counts = await reconciler(clean_tables, settings, provider).sweep()

    assert counts["checked"] == 1
    assert counts["resolved"] == 1
    assert provider.submitted == 1, "the sweep never transmits"


async def test_the_sweep_stops_after_the_configured_ceiling(
    clean_tables: Database,
) -> None:
    """An order the broker cannot account for waits for a person.

    Polling it forever spends a rate-limited budget on a question only a human
    can now answer.
    """
    settings = settings_for(execution_reconcile_max_attempts=2)
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    provider.search = CandidateSearch(pending_ok=False, history_ok=False)

    service = reconciler(clean_tables, settings, provider)
    assert (await service.sweep())["checked"] == 1
    assert (await service.sweep())["checked"] == 1
    # Two inconclusive passes recorded; the third sweep skips it.
    assert (await service.sweep())["checked"] == 0
    attempt = await attempt_of(clean_tables, attempt_id)
    assert attempt.reconciliation_attempts == 2
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS


async def test_reconciliation_never_calls_submit(clean_tables: Database) -> None:
    """The property the whole module exists for, asserted directly."""
    settings = settings_for()
    provider = FakeProvider(_environment="demo")
    _, attempt_id = await ambiguous_attempt(clean_tables, settings, provider)
    before = provider.submitted
    provider.search = CandidateSearch(pending_ok=True, history_ok=True)

    for _ in range(3):
        await reconciler(clean_tables, settings, provider).reconcile(attempt_id)

    assert provider.submitted == before
