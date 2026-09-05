"""One authorized proposal, at most one broker order.

The ordering under test is the whole safety argument, so it is worth restating:
``sent_to_broker`` is committed *before* the HTTP request rather than after the
response, which means a crash anywhere in the send window leaves evidence that
bytes may have left.  Every test here is ultimately about the same question --
**how many times did we transmit?** -- and the answer is never more than once.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    ExecutionFailure,
    ExecutionOutcome,
    ExecutionPolicy,
    JobType,
    OrderSide,
    ProposalStatus,
    ThesisAction,
)
from stockbrain.errors import (
    AmbiguousTransportFailure,
    BrokerRejection,
    DefinitePreSendFailure,
)
from stockbrain.execution.service import ExecutionService
from tests import proposal_helpers as helpers
from tests.execution_helpers import (
    FakeProvider,
    acknowledgement_for,
    authorized,
    build,
    execution_settings,
    order_view,
)

pytestmark = pytest.mark.integration

WEB_ACTOR = "web:local-operator"


async def attempts_of(database: Database, proposal_id: uuid.UUID) -> list[ExecutionAttempt]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(ExecutionAttempt)
                    .where(ExecutionAttempt.proposal_id == proposal_id)
                    .order_by(ExecutionAttempt.attempt_number)
                )
            ).scalars()
        )


async def status_of(database: Database, proposal_id: uuid.UUID) -> ProposalStatus:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal.status


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
async def test_an_authorized_proposal_transmits_exactly_once(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 1
    assert result.transmitted
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.broker_order_id == "500100"

    attempts = await attempts_of(clean_tables, proposal_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.sent_to_broker is True
    assert attempt.sent_at is not None
    assert attempt.outcome is ExecutionOutcome.SUBMITTED
    assert attempt.ambiguous is False
    assert attempt.http_status == 200
    assert attempt.broker_order_id == "500100"
    assert attempt.request_fingerprint
    assert attempt.preflight_at is not None
    # A working order keeps the proposal EXECUTING, which keeps its exposure
    # reserved: an unfilled order is committed cash the snapshot cannot see yet.
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.EXECUTING


async def test_the_command_is_built_from_the_proposal_row_alone(
    clean_tables: Database,
) -> None:
    """No caller supplies a ticker, a side or a quantity.

    The command the adapter receives is compared against the persisted row, so
    a future refactor that started accepting an override would fail here.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    await execution.execute(proposal_id)

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    command = provider.commands[0]
    assert command.broker_ticker == proposal.broker_ticker
    assert command.side is proposal.side
    assert command.quantity == proposal.proposed_quantity
    assert command.broker_environment == proposal.broker_environment
    assert command.signed_quantity == proposal.proposed_quantity  # a BUY


async def test_a_filled_order_marks_the_proposal_executed(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    provider = FakeProvider(_environment="demo")
    provider.acknowledgement = acknowledgement_for(
        order_view(status="FILLED", filled_quantity=Decimal("2"), filled_value=Decimal("400.10"))
    )
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    result = await execution.execute(proposal_id)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.EXECUTED
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        order = (
            await session.execute(
                sa.select(BrokerOrder).where(BrokerOrder.proposal_id == proposal_id)
            )
        ).scalar_one()
    assert proposal is not None and proposal.executed_at is not None
    assert order.broker_status == "FILLED"
    assert order.broker_environment == "demo"
    assert order.initiated_from == "API"
    assert order.is_terminal is True


async def test_a_broker_order_is_mirrored_locally(clean_tables: Database) -> None:
    """The broker stays authoritative; the mirror is derived and marked so."""
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)
    await execution.execute(proposal_id)

    async with clean_tables.session() as session:
        order = (
            await session.execute(
                sa.select(BrokerOrder).where(BrokerOrder.proposal_id == proposal_id)
            )
        ).scalar_one()
        attempt = (
            await session.execute(
                sa.select(ExecutionAttempt).where(ExecutionAttempt.proposal_id == proposal_id)
            )
        ).scalar_one()
    assert order.broker_order_id == "500100"
    assert order.execution_attempt_id == attempt.id
    assert order.discovered_by_reconciliation is False
    assert order.quantity == Decimal("2")


async def test_the_execution_snapshot_records_what_the_send_was_decided_on(
    clean_tables: Database,
) -> None:
    """Immutable, and separate from the proposal's own columns.

    A proposal records the decision that was made; overwriting it with the newer
    world the order was sent into would destroy the only record of what the
    operator actually approved.
    """
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)
    await execution.execute(proposal_id)

    snapshot = (await attempts_of(clean_tables, proposal_id))[0].execution_snapshot
    for key in (
        "proposal_id",
        "broker_environment",
        "provider_environment",
        "configured_environment",
        "broker_instrument_id",
        "broker_ticker",
        "side",
        "quantity",
        "signed_quantity",
        "reference_price",
        "authorization_source",
        "authorized_at",
        "execution_policy",
        "risk_policy_version",
        "preflight_at",
        "quote",
        "account",
        "control",
        "transmission_gates",
        "request_fingerprint",
    ):
        assert key in snapshot, key
    assert snapshot["quote"]["spread_bps"] is not None
    assert snapshot["quote"]["age_ms"] is not None
    assert snapshot["account"]["currency"] == "USD"
    assert snapshot["control"]["trading_halted"] is False
    assert snapshot["transmission_gates"]["order_transmission_permitted"] is True
    # Money and quantities are strings, never binary floats.
    assert isinstance(snapshot["quantity"], str)
    assert isinstance(snapshot["reference_price"], str)


async def test_a_sell_transmits_a_negative_quantity(clean_tables: Database) -> None:
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(
        clean_tables,
        settings,
        action=ThesisAction.SELL,
        positions={"AAPL_US_EQ": (Decimal("10"), Decimal("10"))},
    )
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None and proposal.side is OrderSide.SELL

    await execution.execute(proposal_id)
    assert provider.commands[0].signed_quantity < 0


# ---------------------------------------------------------------------------
# Transmission gates
# ---------------------------------------------------------------------------
async def test_the_master_switch_blocks_transmission(clean_tables: Database) -> None:
    """Deploying Phase 8 must not, by itself, start sending orders."""
    settings = execution_settings(t212_execution_enabled=False)
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert not result.transmitted
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    assert "T212_EXECUTION_ENABLED is false" in result.reason
    # A permission refusal is about the deployment, not about the trade, so the
    # authorization survives it.
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_research_only_mode_blocks_transmission(clean_tables: Database) -> None:
    settings = execution_settings(execution_mode="research_only")
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "EXECUTION_MODE is 'research_only'" in result.reason


async def test_missing_credentials_block_transmission(clean_tables: Database) -> None:
    settings = execution_settings(t212_api_key="", t212_api_secret="")
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "credentials are not configured" in result.reason


async def test_live_transmission_requires_all_four_gates(clean_tables: Database) -> None:
    """Demo permits transmission with the master switch; live does not.

    The four live gates are unchanged from Phase 4 and are simply added to the
    transmission blockers when the environment is live.
    """
    settings = execution_settings(t212_env="live")
    provider = FakeProvider(_environment="live")
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert not result.transmitted
    assert "T212_LIVE_EXECUTION_ENABLED is false" in result.reason
    assert "T212_WRITTEN_CONSENT_CONFIRMED is false" in result.reason


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------
async def test_a_demo_proposal_cannot_reach_a_live_client(
    clean_tables: Database,
) -> None:
    """Fails closed, and never opens a socket.

    Three independent facts must agree: what the proposal was created under,
    what the process is configured for, and what the provider points at.
    """
    settings = execution_settings()
    provider = FakeProvider(_environment="live")
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert "execution provider points at 'live'" in result.reason
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_a_live_proposal_cannot_reach_a_demo_client(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    # Rewrite the proposal's environment as though it had been created live.
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        proposal.broker_environment = "live"

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "configured for 'demo'" in result.reason


async def test_a_configuration_change_after_creation_blocks_transmission(
    clean_tables: Database,
) -> None:
    """A stale worker started under the old ``T212_ENV`` must not send.

    The proposal's environment is immutable; the process's is not. When they
    disagree, the proposal wins and nothing is transmitted.
    """
    settings = execution_settings()
    _, _, provider, proposal_id = await authorized(clean_tables, settings)

    # Rebuild the service as a worker that came up configured for live.
    live_settings = execution_settings(t212_env="live")
    stale_provider = FakeProvider(_environment="live")
    control = ControlStateService(clean_tables)
    stale = ExecutionService(
        clean_tables,
        live_settings,
        proposals=helpers.service_with(clean_tables, live_settings, control=control),
        provider=stale_provider,
        control=control,
    )
    result = await stale.execute(proposal_id)

    assert stale_provider.submitted == 0
    assert provider.submitted == 0
    assert "demo" in result.reason


async def test_the_database_refuses_an_attempt_in_the_wrong_environment(
    clean_tables: Database,
) -> None:
    """Environment isolation is a database fact, not only a service check.

    ``fk_execution_attempts_proposal_environment`` references
    ``trade_proposals (id, broker_environment)``, so a row claiming the other
    environment cannot be written at all -- whatever a mis-wired worker believes.
    """
    settings = execution_settings()
    _, _, _, proposal_id = await authorized(clean_tables, settings)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            session.add(
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=99,
                    broker_environment="live",
                    request_payload={},
                    request_fingerprint="f" * 64,
                    sent_to_broker=False,
                    outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
                )
            )


# ---------------------------------------------------------------------------
# Kill switch and pause
# ---------------------------------------------------------------------------
async def test_the_kill_switch_blocks_transmission(clean_tables: Database) -> None:
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="telegram:4242", source="HUMAN_TELEGRAM", reason="drill"
    )

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert "kill switch" in result.reason
    # The authorization survives an emergency stop: engaging one must not
    # silently destroy every proposal it stops.
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_a_pause_blocks_transmission(clean_tables: Database) -> None:
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    await ControlStateService(clean_tables).pause(actor="a", source="HUMAN_WEB")

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "paused" in result.reason


async def test_a_kill_switch_engaged_after_the_preflight_still_stops_the_send(
    clean_tables: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check runs inside the transaction that authorises transmission.

    An operator who hits the switch while the preflight is fetching a quote must
    still stop this order, so the kill state is re-read from the database in the
    same transaction that would write ``sent_to_broker``.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    control = ControlStateService(clean_tables)
    original = execution.preflight.run

    async def engage_then_run(*args: object, **kwargs: object) -> object:
        outcome = await original(*args, **kwargs)  # type: ignore[arg-type]
        await control.engage_kill_switch(actor="a", source="HUMAN_WEB", reason="race")
        return outcome

    monkeypatch.setattr(execution.preflight, "run", engage_then_run)
    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert "claimed this proposal before transmission" in result.reason
    assert await attempts_of(clean_tables, proposal_id) == []
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


# ---------------------------------------------------------------------------
# Proposal state gates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [
        ProposalStatus.READY,
        ProposalStatus.NOTIFIED,
        ProposalStatus.REJECTED,
        ProposalStatus.EXPIRED,
        ProposalStatus.INVALIDATED,
        ProposalStatus.CANCELLED,
        ProposalStatus.EXECUTED,
        ProposalStatus.FAILED,
    ],
)
async def test_only_an_approved_proposal_may_be_transmitted(
    clean_tables: Database, status: ProposalStatus
) -> None:
    """An authorization is the only licence to send."""
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).where(TradeProposal.id == proposal_id).values(status=status)
        )

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "not executable" in result.reason or "no longer" in result.reason


async def test_an_expired_proposal_is_not_transmitted(clean_tables: Database) -> None:
    """The TTL bounds how stale the approved price may be.

    An authorization does not become a licence to send at any later time, so an
    APPROVED proposal past its expiry is refused rather than sent.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        proposal.expires_at = utcnow() - dt.timedelta(seconds=1)

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "expired" in result.reason


async def test_an_approved_proposal_cannot_exist_without_provenance(
    clean_tables: Database,
) -> None:
    """The executor checks for provenance; the database makes it unnecessary.

    ``ck_trade_proposals_approved_requires_authorization_provenance`` refuses the
    row outright, so "an unauthorized proposal cannot execute" holds even against
    a bug that tried to strip the provenance first. The service-level check
    remains as the readable refusal for a row that somehow arrived another way.
    """
    from sqlalchemy.exc import IntegrityError

    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    with pytest.raises(IntegrityError, match="authorization_provenance"):
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.update(TradeProposal)
                .where(TradeProposal.id == proposal_id)
                .values(authorization_source=None)
            )

    # And the executor's own gate, exercised directly.
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    outcome = await execution.preflight.run(proposal, now=utcnow(), sent_attempt_exists=True)
    assert outcome.refusal is not None
    assert "already been recorded as sent" in outcome.refusal.detail
    assert provider.submitted == 0


# ---------------------------------------------------------------------------
# Fresh risk at send time
# ---------------------------------------------------------------------------
async def test_a_market_that_moved_past_the_drift_limit_invalidates_rather_than_sends(
    clean_tables: Database,
) -> None:
    """Never silently resized, never silently sent.

    The operator approved a quantity at a price. If the price moved past the
    configured drift limit, the trade on offer is no longer the trade that was
    authorized, so the proposal is retired and the pipeline must produce a new
    one.
    """
    settings = execution_settings()
    market = helpers.StubMarketData()
    _, execution, provider, proposal_id = await authorized(
        clean_tables, settings, market_data=market
    )
    market.bid = Decimal("260.00")
    market.ask = Decimal("260.10")

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.INVALIDATED
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.invalidation_reason is not None


async def test_a_spread_that_widened_past_the_ceiling_stops_the_send(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    market = helpers.StubMarketData()
    _, execution, provider, proposal_id = await authorized(
        clean_tables, settings, market_data=market
    )
    market.bid = Decimal("150.00")
    market.ask = Decimal("250.00")

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "spread" in result.reason.lower()
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.INVALIDATED


async def test_a_provider_outage_defers_rather_than_destroying_the_proposal(
    clean_tables: Database,
) -> None:
    """ "We could not check" is not "this trade is wrong".

    Failing every authorized proposal whenever a market-data provider blinks
    would make an outage destructive rather than degrading, so a transient
    refusal leaves the proposal APPROVED for the next sweep.
    """
    from stockbrain.errors import ProviderUnavailable

    settings = execution_settings()
    market = helpers.StubMarketData()
    _, execution, provider, proposal_id = await authorized(
        clean_tables, settings, market_data=market
    )
    market.error = ProviderUnavailable("alpaca is down")

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_a_sale_of_pie_reserved_shares_is_refused(clean_tables: Database) -> None:
    """Shares inside a pie are owned but not individually tradable.

    Phase 6 measured ``quantityAvailableForTrading`` differing from ``quantity``
    on 13 of 14 real positions. This check runs against the *current* snapshot,
    because the pie can move between authorization and transmission.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(
        clean_tables,
        settings,
        action=ThesisAction.SELL,
        positions={"AAPL_US_EQ": (Decimal("10"), Decimal("10"))},
    )
    # The pie swallows almost everything between authorization and send.
    async with clean_tables.transaction() as session:
        from stockbrain.db.models.portfolio import Position

        await session.execute(
            sa.update(Position)
            .where(Position.broker_ticker == "AAPL_US_EQ")
            .values(quantity_available=Decimal("1"))
        )

    result = await execution.execute(proposal_id)
    assert provider.submitted == 0
    assert "available to trade" in result.reason or "position" in result.reason.lower()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
async def test_a_local_rate_limit_denial_defers_without_transmitting(
    clean_tables: Database,
) -> None:
    """Taken before the send transaction, so a denial is provably pre-send."""
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", slot_available=False)
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0
    assert provider.slot_requests == 1
    assert result.failure is ExecutionFailure.RATE_LIMITED_LOCALLY
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    attempts = await attempts_of(clean_tables, proposal_id)
    assert attempts[0].sent_to_broker is False
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_rate_limit_headers_are_recorded_on_a_successful_attempt(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)
    await execution.execute(proposal_id)
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.rate_limit_headers is not None
    assert attempt.rate_limit_headers.get("remaining") == 49


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------
async def test_a_broker_rejection_fails_the_proposal_definitively(
    clean_tables: Database,
) -> None:
    """A complete HTTP response is proof the broker decided: no order exists.

    This is the one failure shape where releasing the reservation is provably
    correct.
    """
    settings = execution_settings()
    provider = FakeProvider(
        _environment="demo",
        error=BrokerRejection("refused", status=400, category="BROKER_REJECTED"),
    )
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.REJECTED_BY_BROKER
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.FAILED
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.http_status == 400
    assert attempt.error_category == "BROKER_REJECTED"
    assert attempt.ambiguous is False
    assert attempt.sent_to_broker is True


async def test_a_proven_pre_send_failure_retracts_the_flag_and_re_arms(
    clean_tables: Database,
) -> None:
    """The only place ``sent_to_broker`` is ever set back to false.

    The flag records "bytes may have left"; a ``DefinitePreSendFailure`` is proof
    they did not. Leaving it set would permanently strand an authorized proposal
    the broker has never heard of -- and the partial unique index would block
    every future attempt.
    """
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", error=DefinitePreSendFailure("connection refused"))
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 1
    assert not result.transmitted
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.sent_to_broker is False
    assert attempt.sent_at is None
    assert attempt.error_category == ExecutionFailure.CONNECT_FAILED.value
    # The authorization still stands, so the sweep can try again.
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.APPROVED

    async with clean_tables.session() as session:
        entry = (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.action == "execution.retracted_unsent")
            )
        ).scalar_one()
    assert entry.details["sent_to_broker_retracted"] is True


async def test_a_re_armed_proposal_can_transmit_on_the_next_attempt(
    clean_tables: Database,
) -> None:
    """The retraction frees the partial index for a genuine second attempt."""
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", error=DefinitePreSendFailure("connection refused"))
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    await execution.execute(proposal_id)

    provider.error = None
    result = await execution.execute(proposal_id)

    assert provider.submitted == 2
    assert result.outcome is ExecutionOutcome.SUBMITTED
    attempts = await attempts_of(clean_tables, proposal_id)
    assert len(attempts) == 2
    assert [attempt.sent_to_broker for attempt in attempts] == [False, True]


async def test_an_ambiguous_transport_failure_never_resends(
    clean_tables: Database,
) -> None:
    """The order may exist.  Reconciliation decides; nothing is retried.

    The proposal keeps reserving its exposure, because until the outcome is
    known the cash may be committed at the broker.
    """
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", error=AmbiguousTransportFailure("read timed out"))
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)

    result = await execution.execute(proposal_id)

    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert result.reconcile_required
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.EXECUTION_AMBIGUOUS
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.sent_to_broker is True
    assert attempt.ambiguous is True
    assert attempt.error_category == ExecutionFailure.TRANSPORT_AMBIGUOUS.value

    # A reconciliation job was queued, and no second execution job.
    async with clean_tables.session() as session:
        from stockbrain.db.models.system import Job

        jobs = [row.job_type for row in (await session.execute(sa.select(Job))).scalars()]
    assert JobType.RECONCILE_EXECUTION.value in jobs


async def test_a_second_execution_of_an_ambiguous_proposal_never_transmits(
    clean_tables: Database,
) -> None:
    """A redelivered job on an ambiguous proposal reconciles, never resends."""
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", error=AmbiguousTransportFailure("read timed out"))
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    await execution.execute(proposal_id)
    provider.error = None

    result = await execution.execute(proposal_id)

    assert provider.submitted == 1, "no second transmission, ever"
    assert "already been transmitted" in result.reason
    assert len(await attempts_of(clean_tables, proposal_id)) == 1


async def test_an_unreadable_success_is_ambiguous_and_reconciles(
    clean_tables: Database,
) -> None:
    """The broker accepted the order and its id is unknown."""
    settings = execution_settings()
    provider = FakeProvider(
        _environment="demo",
        error=AmbiguousTransportFailure(
            "trading212: the order was accepted but the response could not be parsed"
        ),
    )
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    result = await execution.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.error_category == ExecutionFailure.UNREADABLE_SUCCESS.value
    assert attempt.broker_order_id is None


async def test_an_unexpected_exception_is_treated_as_ambiguous(
    clean_tables: Database,
) -> None:
    """Ambiguity is the only honest answer, and also the safe one."""
    settings = execution_settings()
    provider = FakeProvider(_environment="demo", error=RuntimeError("something odd"))
    _, execution, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    result = await execution.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.EXECUTION_AMBIGUOUS


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------
async def test_a_crash_after_the_send_flag_reconciles_instead_of_resending(
    clean_tables: Database,
) -> None:
    """Crash boundary B: the flag is committed, the request never happened.

    This is the cost of the ordering, paid deliberately: the outcome is unknown
    even though nothing was sent, and unknown means reconcile.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    # Exactly the row the send transaction commits, with the process then dying.
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        session.add(
            ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=1,
                broker_environment=proposal.broker_environment,
                request_payload={"broker_ticker": "AAPL_US_EQ", "signed_quantity": "2"},
                request_fingerprint="a" * 64,
                sent_to_broker=True,
                sent_at=utcnow(),
                outcome=ExecutionOutcome.PENDING,
            )
        )
        proposal.status = ProposalStatus.EXECUTING

    result = await execution.execute(proposal_id)

    assert provider.submitted == 0, "a restart must never resend"
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert result.failure is ExecutionFailure.CRASH_RECOVERY
    assert await status_of(clean_tables, proposal_id) is ProposalStatus.EXECUTION_AMBIGUOUS


async def test_the_recovery_sweep_finds_stranded_attempts(
    clean_tables: Database,
) -> None:
    """A worker can die without the process doing so, so this runs on a timer."""
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        session.add(
            ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=1,
                broker_environment=proposal.broker_environment,
                request_payload={"broker_ticker": "AAPL_US_EQ", "signed_quantity": "2"},
                request_fingerprint="b" * 64,
                sent_to_broker=True,
                sent_at=utcnow() - dt.timedelta(minutes=5),
                outcome=ExecutionOutcome.PENDING,
            )
        )
        proposal.status = ProposalStatus.EXECUTING

    stranded = await execution.recover_incomplete()

    assert stranded == 1
    assert provider.submitted == 0
    attempt = (await attempts_of(clean_tables, proposal_id))[0]
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS
    assert attempt.error_category == ExecutionFailure.CRASH_RECOVERY.value


async def test_a_recent_pending_attempt_is_left_alone_by_the_sweep(
    clean_tables: Database,
) -> None:
    """An in-flight request is not a stranded one.

    The cutoff is twice the order timeout, so a request that is merely slow is
    not declared ambiguous underneath the worker still waiting for it.
    """
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        session.add(
            ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=1,
                broker_environment=proposal.broker_environment,
                request_payload={},
                request_fingerprint="c" * 64,
                sent_to_broker=True,
                sent_at=utcnow(),
                outcome=ExecutionOutcome.PENDING,
            )
        )
        proposal.status = ProposalStatus.EXECUTING

    assert await execution.recover_incomplete() == 0


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
async def test_ready_proposals_are_enqueued_once(clean_tables: Database) -> None:
    """Derived from the database, so a restart resumes where it left off."""
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)

    assert await execution.enqueue_ready() == 1
    # The job dedupe key makes a second enqueue a no-op.
    assert await execution.enqueue_ready() == 0

    async with clean_tables.session() as session:
        from stockbrain.db.models.system import Job

        jobs = list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.EXECUTE_PROPOSAL.value)
                )
            ).scalars()
        )
    assert len(jobs) == 1
    assert jobs[0].payload["proposal_id"] == str(proposal_id)
    assert jobs[0].dedupe_key == f"execute:{proposal_id}"


async def test_nothing_is_enqueued_while_halted_or_ungated(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    _, execution, _, _ = await authorized(clean_tables, settings)
    await ControlStateService(clean_tables).pause(actor="a", source="HUMAN_WEB")
    assert await execution.enqueue_ready() == 0

    await ControlStateService(clean_tables).resume(actor="a", source="HUMAN_WEB")
    blocked_settings = execution_settings(t212_execution_enabled=False)
    _, blocked, _ = build(clean_tables, blocked_settings)
    assert await blocked.enqueue_ready() == 0


async def test_a_transmitted_proposal_is_not_enqueued_again(
    clean_tables: Database,
) -> None:
    settings = execution_settings()
    _, execution, _, proposal_id = await authorized(clean_tables, settings)
    await execution.execute(proposal_id)
    assert await execution.enqueue_ready() == 0


async def test_an_automatic_proposal_uses_the_same_engine(
    clean_tables: Database,
) -> None:
    """MANUAL and AUTOMATIC differ in provenance, not in execution path.

    Both reach ``APPROVED`` and both are transmitted by this one service, so
    there is no second broker path that could diverge.
    """
    settings = execution_settings(execution_policy=ExecutionPolicy.AUTOMATIC)
    assert settings.automatic_authorization_permitted, settings.automation_blockers
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.authorization_source is AuthorizationSource.SYSTEM_AUTOMATIC

    result = await execution.execute(proposal_id)
    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.SUBMITTED
    snapshot = (await attempts_of(clean_tables, proposal_id))[0].execution_snapshot
    assert snapshot["authorization_source"] == "SYSTEM_AUTOMATIC"
    assert snapshot["execution_policy"] == "AUTOMATIC"


async def test_automatic_live_execution_rechecks_the_consent_flag_at_send_time(
    clean_tables: Database,
) -> None:
    """Not merely at authorization.

    Phase 6 already refuses to *start* with ``EXECUTION_POLICY=automatic`` and
    ``T212_ENV=live`` and no consent -- the settings model rejects that
    combination outright, so this test cannot construct it. What it can
    construct is the case the send-time check actually exists for: a proposal
    authorized ``SYSTEM_AUTOMATIC`` while the deployment was demo, reaching a
    process configured for live without the consent flag. Clause 4.2(a) is about
    the moment an order is determined and sent, so the check runs then and not
    only when the authorization was granted.
    """
    settings = execution_settings(execution_policy=ExecutionPolicy.AUTOMATIC)
    _, _, _, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.authorization_source is AuthorizationSource.SYSTEM_AUTOMATIC
    assert proposal.execution_policy is ExecutionPolicy.AUTOMATIC

    # The proposal is re-bound to live so the environment check does not mask
    # the consent check; a live account snapshot goes with it.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal)
            .where(TradeProposal.id == proposal_id)
            .values(broker_environment="live")
        )
    await helpers.fund(clean_tables, environment="live")

    # A legal live configuration: MANUAL policy, so the settings model permits
    # it, with automated-trading consent absent.
    live_settings = execution_settings(
        t212_env="live",
        t212_live_execution_enabled=False,
        t212_automated_trading_consent_confirmed=False,
    )
    provider = FakeProvider(_environment="live")
    control = ControlStateService(clean_tables)
    executor = ExecutionService(
        clean_tables,
        live_settings,
        proposals=helpers.service_with(clean_tables, live_settings, control=control),
        provider=provider,
        control=control,
    )
    result = await executor.execute(proposal_id)

    assert provider.submitted == 0
    assert "T212_AUTOMATED_TRADING_CONSENT_CONFIRMED" in result.reason
    # And the four live gates are in the same refusal, independently.
    assert "T212_LIVE_EXECUTION_ENABLED is false" in result.reason


async def test_a_manual_proposal_needs_no_automation_consent(
    clean_tables: Database,
) -> None:
    """The consent flag gates *automatic* determination, not every order.

    A human-authorized proposal in demo transmits without it, which is the
    distinction clause 4.2(a) actually draws.
    """
    settings = execution_settings(t212_automated_trading_consent_confirmed=False)
    _, execution, provider, proposal_id = await authorized(
        clean_tables, settings, source=AuthorizationSource.HUMAN_TELEGRAM
    )
    result = await execution.execute(proposal_id)
    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.SUBMITTED
    snapshot = (await attempts_of(clean_tables, proposal_id))[0].execution_snapshot
    assert snapshot["authorization_source"] == "HUMAN_TELEGRAM"
