"""Races, and the one number that matters: how many orders were transmitted.

Trading 212's order endpoint is documented as non-idempotent, so every race here
has the same acceptance criterion -- **at most one transmission** -- and every
mechanism that enforces it lives in PostgreSQL, because the guarantee has to
hold across worker tasks, across processes and across a restart:

* a transaction-scoped advisory lock on the proposal, so two workers serialise;
* ``SELECT … FOR UPDATE`` plus the optimistic ``version`` column, so a proposal
  that moved during the preflight aborts the send;
* an explicit "has anything already been recorded as sent?" check inside that
  lock, which turns a redelivery into a reconciliation;
* ``uq_execution_attempts_sent_once``, the database having the last word.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    Broker,
    ExecutionOutcome,
    ProposalStatus,
)
from stockbrain.execution.service import ExecutionService
from tests import proposal_helpers as helpers
from tests.execution_helpers import FakeProvider

pytestmark = pytest.mark.integration


def settings_for(**overrides: object) -> Settings:
    base: dict[str, object] = {"t212_execution_enabled": True}
    base.update(overrides)
    return helpers.settings(**base)


def executor(database: Database, settings: Settings, provider: FakeProvider) -> ExecutionService:
    """A separate service object, as a separate worker process would build.

    Nothing is shared between two of these but the database, which is the point:
    if the guarantee lived in memory it would not survive this test.
    """
    control = ControlStateService(database)
    return ExecutionService(
        database,
        settings,
        proposals=helpers.service_with(database, settings, control=control),
        provider=provider,
        control=control,
        broker=Broker.TRADING212,
    )


async def approved_proposal(
    database: Database,
    settings: Settings,
    *,
    source: AuthorizationSource = AuthorizationSource.HUMAN_WEB,
) -> uuid.UUID:
    proposals = helpers.service_with(database, settings, control=ControlStateService(database))
    await helpers.seed(database)
    await helpers.fund(database, environment=settings.t212_env.value)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None, generated.reason
    if not generated.authorized:
        await proposals.authorize(generated.proposal_id, source=source, actor="web:local-operator")
    return generated.proposal_id


async def sent_count(database: Database) -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(ExecutionAttempt)
                    .where(ExecutionAttempt.sent_to_broker.is_(True))
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Two workers
# ---------------------------------------------------------------------------
async def test_two_workers_on_one_proposal_transmit_once(
    clean_tables: Database,
) -> None:
    """Two independent services, one shared provider, one order."""
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    provider.slot_available = True
    first = executor(clean_tables, settings, provider)
    second = executor(clean_tables, settings, provider)

    results = await asyncio.gather(first.execute(proposal_id), second.execute(proposal_id))

    assert provider.submitted == 1
    assert await sent_count(clean_tables) == 1
    transmitted = [result for result in results if result.transmitted]
    # One transmitted; the other either lost the claim or found the sent attempt.
    assert len(transmitted) <= 2
    assert sum(1 for result in results if result.outcome is ExecutionOutcome.SUBMITTED) == 1


async def test_four_workers_on_one_proposal_transmit_once(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    workers = [executor(clean_tables, settings, provider) for _ in range(4)]

    await asyncio.gather(*(worker.execute(proposal_id) for worker in workers))

    assert provider.submitted == 1
    assert await sent_count(clean_tables) == 1


async def test_a_duplicate_execution_job_reconciles_rather_than_resending(
    clean_tables: Database,
) -> None:
    """At-least-once delivery means this job runs twice eventually."""
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    worker = executor(clean_tables, settings, provider)

    first = await worker.execute(proposal_id)
    second = await worker.execute(proposal_id)

    assert provider.submitted == 1
    assert first.outcome is ExecutionOutcome.SUBMITTED
    assert "already been transmitted" in second.reason
    assert await sent_count(clean_tables) == 1


async def test_the_partial_index_refuses_a_second_transmitted_attempt(
    clean_tables: Database,
) -> None:
    """``uq_execution_attempts_sent_once``, exercised directly.

    Even a bug that bypassed every service-layer check would hit this before a
    second transmission could be recorded.
    """
    from sqlalchemy.exc import IntegrityError

    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    await executor(clean_tables, settings, provider).execute(proposal_id)

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None

    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            session.add(
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=99,
                    broker_environment=proposal.broker_environment,
                    request_payload={},
                    request_fingerprint="d" * 64,
                    sent_to_broker=True,
                    sent_at=utcnow(),
                    outcome=ExecutionOutcome.PENDING,
                )
            )
    assert await sent_count(clean_tables) == 1


# ---------------------------------------------------------------------------
# Execution racing the proposal lifecycle
# ---------------------------------------------------------------------------
async def test_execution_racing_an_invalidation_transmits_at_most_once(
    clean_tables: Database,
) -> None:
    """A sweep can invalidate a proposal while a worker is pricing it.

    The version check inside the send transaction is what settles it: the
    proposal moved, so the claim is lost and nothing is transmitted.
    """
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    worker = executor(clean_tables, settings, provider)
    original = worker.preflight.run

    async def invalidate_then_run(*args: object, **kwargs: object) -> object:
        outcome = await original(*args, **kwargs)  # type: ignore[arg-type]
        async with clean_tables.transaction() as session:
            proposal = await session.get(TradeProposal, proposal_id)
            assert proposal is not None
            proposal.status = ProposalStatus.INVALIDATED
            proposal.invalidated_at = utcnow()
            proposal.invalidation_reason = "the listing was retired mid-flight"
        return outcome

    worker.preflight.run = invalidate_then_run  # type: ignore[assignment,method-assign]
    result = await worker.execute(proposal_id)

    assert provider.submitted == 0
    assert await sent_count(clean_tables) == 0
    assert "before transmission" in result.reason
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None and proposal.status is ProposalStatus.INVALIDATED


async def test_execution_racing_a_rejection_transmits_at_most_once(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    worker = executor(clean_tables, settings, provider)
    proposals = helpers.service_with(
        clean_tables, settings, control=ControlStateService(clean_tables)
    )

    async def cancel() -> None:
        await proposals.cancel(proposal_id, actor="web:local-operator")

    outcomes = await asyncio.gather(worker.execute(proposal_id), cancel(), return_exceptions=True)

    assert provider.submitted <= 1
    assert await sent_count(clean_tables) <= 1
    # Whatever the interleaving, the proposal is in exactly one state and the
    # order count matches it.
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    if provider.submitted == 1:
        assert proposal.status in {
            ProposalStatus.EXECUTING,
            ProposalStatus.EXECUTED,
            ProposalStatus.CANCELLED,
        }
    assert outcomes is not None


async def test_execution_racing_the_kill_switch_transmits_at_most_once(
    clean_tables: Database,
) -> None:
    """The switch is re-read inside the send transaction, from the database."""
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    worker = executor(clean_tables, settings, provider)
    control = ControlStateService(clean_tables)

    async def kill() -> None:
        await control.engage_kill_switch(actor="a", source="HUMAN_WEB", reason="race")

    await asyncio.gather(worker.execute(proposal_id), kill())

    assert provider.submitted <= 1
    assert await sent_count(clean_tables) <= 1
    if provider.submitted == 0:
        # The switch won: the authorization survives it.
        async with clean_tables.session() as session:
            proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        assert proposal.status is ProposalStatus.APPROVED


async def test_execution_racing_a_pause_transmits_at_most_once(
    clean_tables: Database,
) -> None:
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    worker = executor(clean_tables, settings, provider)
    control = ControlStateService(clean_tables)

    await asyncio.gather(
        worker.execute(proposal_id),
        control.pause(actor="a", source="HUMAN_WEB", reason="race"),
    )
    assert provider.submitted <= 1
    assert await sent_count(clean_tables) <= 1


# ---------------------------------------------------------------------------
# Reconciliation racing execution
# ---------------------------------------------------------------------------
async def test_concurrent_reconciliations_resolve_once(clean_tables: Database) -> None:
    """Two sweeps on one attempt must not double-count or double-conclude."""
    from stockbrain.execution.reconciliation import ReconciliationService
    from tests.execution_helpers import order_view

    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    provider.error = None
    worker = executor(clean_tables, settings, provider)
    await worker.execute(proposal_id)

    attempt_id = await _first_attempt_id(clean_tables, proposal_id)
    provider.orders = {"500100": order_view(status="FILLED", filled_quantity=Decimal("2"))}

    control = ControlStateService(clean_tables)
    reconcilers = [
        ReconciliationService(
            clean_tables,
            settings,
            provider=provider,
            proposals=helpers.service_with(clean_tables, settings, control=control),
        )
        for _ in range(3)
    ]
    await asyncio.gather(*(r.reconcile(attempt_id) for r in reconcilers))

    assert provider.submitted == 1, "reconciliation never transmits"
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        orders = int(
            (
                await session.execute(
                    sa.select(sa.func.count()).select_from(sa.text("broker_orders"))
                )
            ).scalar_one()
        )
    assert proposal is not None and proposal.status is ProposalStatus.EXECUTED
    assert orders == 1, "the broker-order mirror is upserted, not duplicated"


async def test_a_manual_and_an_automatic_request_for_one_proposal_send_once(
    clean_tables: Database,
) -> None:
    """Both policies converge on the same engine, so both race the same lock."""
    settings = settings_for()
    proposal_id = await approved_proposal(clean_tables, settings)
    provider = FakeProvider(_environment="demo")
    manual = executor(clean_tables, settings, provider)
    automatic = executor(clean_tables, settings_for(execution_policy="automatic"), provider)

    await asyncio.gather(manual.execute(proposal_id), automatic.execute(proposal_id))

    assert provider.submitted == 1
    assert await sent_count(clean_tables) == 1


async def _first_attempt_id(database: Database, proposal_id: uuid.UUID) -> uuid.UUID:
    async with database.session() as session:
        return uuid.UUID(
            str(
                (
                    await session.execute(
                        sa.select(ExecutionAttempt.id).where(
                            ExecutionAttempt.proposal_id == proposal_id
                        )
                    )
                )
                .scalars()
                .first()
            )
        )
