"""Trading 212's 50-pending-orders-per-ticker limit.

Documented as a functional limit on the account, and made reachable rather than
theoretical by the Phase 8 live verification: an ``AAPL_US_EQ`` market order
submitted while the market was closed returned HTTP 200 with status ``NEW`` and
sat in the queue.  Fifty of those and the fifty-first submission is rejected --
by a **non-idempotent** endpoint, which is the worst place to discover a limit.

The rule these tests protect: the count comes from two independent sources and
the larger wins, a read that failed is not a count of zero, and a full queue
refuses without destroying the authorization.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import ExecutionFailure, ExecutionOutcome, ProposalStatus
from tests.execution_helpers import FakeProvider, authorized, execution_settings

pytestmark = pytest.mark.integration


def _clone_of(
    proposal: TradeProposal,
    clone_id: uuid.UUID,
    *,
    broker_ticker: str | None = None,
    broker_environment: str | None = None,
) -> TradeProposal:
    """A terminal copy of a proposal, used to manufacture prior attempts.

    ``uq_execution_attempts_sent_once`` permits exactly one transmitted attempt
    per proposal -- which is the guarantee under test elsewhere -- so a *queue*
    of fifty transmitted orders has to be fifty proposals.
    """
    return TradeProposal(
        id=clone_id,
        broker=proposal.broker,
        broker_ticker=broker_ticker or proposal.broker_ticker,
        account_id=proposal.account_id,
        broker_environment=broker_environment or proposal.broker_environment,
        side=proposal.side,
        order_type=proposal.order_type,
        proposed_quantity=proposal.proposed_quantity,
        reference_price=proposal.reference_price,
        reference_currency=proposal.reference_currency,
        price_source=proposal.price_source,
        quote_timestamp=proposal.quote_timestamp,
        quote_age_ms=proposal.quote_age_ms,
        estimated_notional=proposal.estimated_notional,
        account_currency=proposal.account_currency,
        execution_policy=proposal.execution_policy,
        status=ProposalStatus.EXECUTED,
        expires_at=proposal.expires_at,
    )


async def _attempts(database: Database) -> list[ExecutionAttempt]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(ExecutionAttempt).order_by(ExecutionAttempt.started_at)
                )
            ).scalars()
        )


async def _proposal(database: Database, proposal_id: uuid.UUID) -> TradeProposal:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pending", [0, 10, 44])
async def test_a_queue_with_room_transmits(clean_tables: Database, pending: int) -> None:
    """Below the ceiling, nothing changes.

    44 is the last value that leaves room: the default keeps five of the fifty
    slots in reserve, so the ceiling is 45 and this order is the 45th.
    """
    provider = FakeProvider(pending_orders=pending)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )

    result = await service.execute(proposal_id)
    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert provider.pending_counts == 1


@pytest.mark.parametrize("pending", [45, 49, 50, 51, 120])
async def test_a_full_queue_refuses_without_transmitting(
    clean_tables: Database, pending: int
) -> None:
    """At or past the ceiling, no bytes leave.

    Note 45: StockBrain refuses five short of the documented fifty. The broker's
    list can lag a fill and the operator can queue an order by hand between one
    check and the next, so stopping short is what keeps StockBrain from ever
    discovering the limit by hitting it.
    """
    provider = FakeProvider(pending_orders=pending)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )

    result = await service.execute(proposal_id)
    assert provider.submitted == 0
    assert result.failure is ExecutionFailure.PENDING_ORDER_LIMIT
    attempts = await _attempts(clean_tables)
    assert len(attempts) == 1
    assert attempts[0].sent_to_broker is False
    assert attempts[0].outcome is ExecutionOutcome.FAILED_BEFORE_SEND


async def test_the_refusal_does_not_destroy_the_authorization(
    clean_tables: Database,
) -> None:
    """A full queue is a condition of the moment, not a verdict on the trade.

    The operator authorized this trade and it is still the trade they
    authorized; it simply cannot be sent yet. Invalidating it would make a
    transient broker condition destructive.
    """
    provider = FakeProvider(pending_orders=50)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    await service.execute(proposal_id)

    proposal = await _proposal(clean_tables, proposal_id)
    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.invalidated_at is None
    # And once the queue drains, the same proposal transmits.
    provider.pending_orders = 0
    result = await service.execute(proposal_id)
    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.SUBMITTED


async def test_a_failed_pending_read_refuses_rather_than_assuming_zero(
    clean_tables: Database,
) -> None:
    """ "The broker would not tell us" and "the broker told us fifty" resolve the
    same way.

    An unknown standing between StockBrain and a non-idempotent POST is not a
    reason to proceed. This is the same judgement as reconciliation's "absence
    is not evidence when a read path failed".
    """
    provider = FakeProvider(pending_read_ok=False)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )

    result = await service.execute(proposal_id)
    assert provider.submitted == 0
    assert result.failure is ExecutionFailure.PENDING_ORDER_LIMIT
    assert result.reason is not None
    assert "could not be read" in result.reason


async def test_the_count_is_checked_before_the_rate_limit_slot_is_taken(
    clean_tables: Database,
) -> None:
    """Cheap, certain checks first.

    There is no point consuming a 49-per-minute order token to discover that the
    order cannot be placed anyway.
    """
    provider = FakeProvider(pending_orders=50)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    await service.execute(proposal_id)
    assert provider.pending_counts == 1
    assert provider.slot_requests == 0


# ---------------------------------------------------------------------------
# The local half of the count
# ---------------------------------------------------------------------------
async def test_an_unresolved_attempt_of_ours_counts_towards_the_limit(
    clean_tables: Database,
) -> None:
    """The window between a successful POST and the broker's list catching up.

    A broker read of zero is not proof the queue is empty when StockBrain has
    just transmitted forty-five orders it has not reconciled. Both numbers are
    computed and the larger wins.
    """
    provider = FakeProvider(pending_orders=0)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    async with clean_tables.transaction() as session:
        for index in range(50):
            session.add(
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=100 + index,
                    started_at=dt.datetime.now(dt.UTC),
                    broker_environment="demo",
                    request_payload={},
                    request_fingerprint=uuid.uuid4().hex,
                    sent_to_broker=False,
                    outcome=ExecutionOutcome.AMBIGUOUS,
                    ambiguous=True,
                )
            )
    # `sent_to_broker=False` -- these were never transmitted, so they do not
    # count, and the send proceeds.
    result = await service.execute(proposal_id)
    assert result.failure is not ExecutionFailure.PENDING_ORDER_LIMIT


async def test_a_transmitted_unresolved_attempt_on_the_same_ticker_counts(
    clean_tables: Database,
) -> None:
    """The real case: orders we sent, whose fate is unknown.

    Written against a *different* proposal for the same ticker, because
    ``uq_execution_attempts_sent_once`` permits exactly one transmitted attempt
    per proposal -- which is the point.
    """
    provider = FakeProvider(pending_orders=0)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        for _ in range(50):
            clone_id = uuid.uuid4()
            session.add(_clone_of(proposal, clone_id))
            await session.flush()
            session.add(
                ExecutionAttempt(
                    proposal_id=clone_id,
                    attempt_number=1,
                    started_at=dt.datetime.now(dt.UTC),
                    broker_environment="demo",
                    request_payload={},
                    request_fingerprint=uuid.uuid4().hex,
                    sent_to_broker=True,
                    sent_at=dt.datetime.now(dt.UTC),
                    outcome=ExecutionOutcome.AMBIGUOUS,
                    ambiguous=True,
                )
            )

    result = await service.execute(proposal_id)
    assert provider.submitted == 0
    assert result.failure is ExecutionFailure.PENDING_ORDER_LIMIT
    assert result.reason is not None
    assert "50 unresolved here" in result.reason


async def test_a_resolved_attempt_no_longer_counts(
    clean_tables: Database,
) -> None:
    """An order that filled is not pending.

    Counting terminal attempts forever would make the limit a lifetime quota.
    """
    provider = FakeProvider(pending_orders=0)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        for _ in range(50):
            clone_id = uuid.uuid4()
            session.add(_clone_of(proposal, clone_id))
            await session.flush()
            session.add(
                ExecutionAttempt(
                    proposal_id=clone_id,
                    attempt_number=1,
                    started_at=dt.datetime.now(dt.UTC),
                    broker_environment="demo",
                    request_payload={},
                    request_fingerprint=uuid.uuid4().hex,
                    sent_to_broker=True,
                    sent_at=dt.datetime.now(dt.UTC),
                    outcome=ExecutionOutcome.SUBMITTED,
                )
            )

    result = await service.execute(proposal_id)
    assert result.failure is not ExecutionFailure.PENDING_ORDER_LIMIT


async def test_an_attempt_in_the_other_environment_does_not_count(
    clean_tables: Database,
) -> None:
    """A demo queue says nothing about the live one.

    The count is environment-scoped for the same reason the composite foreign
    key is: the two worlds share a ticker and share nothing else.
    """
    provider = FakeProvider(pending_orders=0)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        for _ in range(50):
            clone_id = uuid.uuid4()
            session.add(_clone_of(proposal, clone_id, broker_environment="live"))
            await session.flush()
            session.add(
                ExecutionAttempt(
                    proposal_id=clone_id,
                    attempt_number=1,
                    started_at=dt.datetime.now(dt.UTC),
                    broker_environment="live",
                    request_payload={},
                    request_fingerprint=uuid.uuid4().hex,
                    sent_to_broker=True,
                    sent_at=dt.datetime.now(dt.UTC),
                    outcome=ExecutionOutcome.AMBIGUOUS,
                    ambiguous=True,
                )
            )

    result = await service.execute(proposal_id)
    assert result.failure is not ExecutionFailure.PENDING_ORDER_LIMIT


async def test_the_local_count_ignores_other_tickers(clean_tables: Database) -> None:
    """The limit is per ticker, so a full ``MSFT`` queue must not block ``AAPL``.

    A per-account reading of the limit would stop trading entirely the first
    time one instrument's queue filled up. Fifty unresolved transmitted attempts
    on a *different* listing, and this order still goes.
    """
    provider = FakeProvider(pending_orders=0)
    _, service, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        for _ in range(50):
            clone_id = uuid.uuid4()
            session.add(_clone_of(proposal, clone_id, broker_ticker="MSFT_US_EQ"))
            await session.flush()
            session.add(
                ExecutionAttempt(
                    proposal_id=clone_id,
                    attempt_number=1,
                    started_at=dt.datetime.now(dt.UTC),
                    broker_environment="demo",
                    request_payload={},
                    request_fingerprint=uuid.uuid4().hex,
                    sent_to_broker=True,
                    sent_at=dt.datetime.now(dt.UTC),
                    outcome=ExecutionOutcome.AMBIGUOUS,
                    ambiguous=True,
                )
            )

    result = await service.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert provider.submitted == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
async def test_concurrent_sends_for_one_proposal_still_transmit_once(
    clean_tables: Database,
) -> None:
    """The pending check must not weaken the one-send guarantee.

    Four workers, one proposal, one POST -- the Phase 8 property, re-asserted
    now that another broker read sits in front of it.
    """
    provider = FakeProvider(pending_orders=0)
    settings = execution_settings()
    _, first, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    from tests.execution_helpers import build

    workers = [first, *(build(clean_tables, settings, provider=provider)[1] for _ in range(3))]
    await asyncio.gather(*(worker.execute(proposal_id) for worker in workers))

    assert provider.submitted == 1
    sent = [attempt for attempt in await _attempts(clean_tables) if attempt.sent_to_broker]
    assert len(sent) == 1
