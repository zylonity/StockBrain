"""Database-level safety invariants.

These are integration tests on purpose: the guarantees under test are partial
unique indexes and row locks in PostgreSQL, not application logic.  If the
service layer were ever bypassed by a bug, these are what still prevent a
duplicate broker order.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models import (
    ApprovalAction,
    ExecutionAttempt,
    Job,
    Source,
    TradeProposal,
)
from stockbrain.db.session import Database
from stockbrain.enums import (
    ApprovalChannel,
    ApprovalStage,
    Broker,
    ExecutionOutcome,
    JobStatus,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    SourceProvider,
)

pytestmark = pytest.mark.integration


def _proposal(
    *,
    ticker: str = "AAPL_US_EQ",
    status: ProposalStatus = ProposalStatus.READY,
) -> TradeProposal:
    now = utcnow()
    return TradeProposal(
        broker=Broker.TRADING212,
        broker_ticker=ticker,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("0.4237"),
        reference_price=Decimal("176.4200"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=now,
        quote_age_ms=120,
        estimated_notional=Decimal("74.7500"),
        account_currency="GBP",
        status=status,
        expires_at=now + dt.timedelta(minutes=20),
    )


async def _add(session: AsyncSession, obj: object) -> None:
    session.add(obj)
    await session.flush()


async def test_only_one_execution_attempt_can_be_sent_per_proposal(
    clean_tables: Database,
) -> None:
    """The non-idempotent broker POST is protected by the database itself."""
    async with clean_tables.transaction() as session:
        proposal = _proposal(status=ProposalStatus.EXECUTING)
        await _add(session, proposal)
        await _add(
            session,
            ExecutionAttempt(
                proposal_id=proposal.id,
                attempt_number=1,
                broker_environment="demo",
                request_fingerprint="f" * 64,
                sent_to_broker=True,
                sent_at=utcnow(),
                outcome=ExecutionOutcome.SUBMITTED,
            ),
        )
        proposal_id = proposal.id

    with pytest.raises(IntegrityError) as excinfo:
        async with clean_tables.transaction() as session:
            await _add(
                session,
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=2,
                    broker_environment="demo",
                    request_fingerprint="e" * 64,
                    sent_to_broker=True,
                    sent_at=utcnow(),
                    outcome=ExecutionOutcome.PENDING,
                ),
            )
    assert "uq_execution_attempts_sent_once" in str(excinfo.value)


async def test_a_second_unsent_attempt_is_allowed_after_a_pre_send_failure(
    clean_tables: Database,
) -> None:
    """Only a failure that provably never reached the broker permits a new attempt."""
    async with clean_tables.transaction() as session:
        proposal = _proposal(status=ProposalStatus.EXECUTING)
        await _add(session, proposal)
        await _add(
            session,
            ExecutionAttempt(
                proposal_id=proposal.id,
                attempt_number=1,
                broker_environment="demo",
                request_fingerprint="a" * 64,
                sent_to_broker=False,
                outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
            ),
        )
        await _add(
            session,
            ExecutionAttempt(
                proposal_id=proposal.id,
                attempt_number=2,
                broker_environment="demo",
                request_fingerprint="b" * 64,
                sent_to_broker=True,
                sent_at=utcnow(),
                outcome=ExecutionOutcome.SUBMITTED,
            ),
        )


async def test_sent_attempt_requires_a_sent_timestamp(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        proposal = _proposal(status=ProposalStatus.EXECUTING)
        await _add(session, proposal)
        proposal_id = proposal.id

    with pytest.raises(IntegrityError) as excinfo:
        async with clean_tables.transaction() as session:
            await _add(
                session,
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=1,
                    broker_environment="demo",
                    request_fingerprint="c" * 64,
                    sent_to_broker=True,
                    sent_at=None,
                    outcome=ExecutionOutcome.PENDING,
                ),
            )
    assert "sent_requires_timestamp" in str(excinfo.value)


async def test_duplicate_attempt_numbers_are_rejected(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        proposal = _proposal(status=ProposalStatus.EXECUTING)
        await _add(session, proposal)
        await _add(
            session,
            ExecutionAttempt(
                proposal_id=proposal.id,
                attempt_number=1,
                broker_environment="demo",
                request_fingerprint="d" * 64,
                outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
            ),
        )
        proposal_id = proposal.id

    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            await _add(
                session,
                ExecutionAttempt(
                    proposal_id=proposal_id,
                    attempt_number=1,
                    broker_environment="demo",
                    request_fingerprint="g" * 64,
                    outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
                ),
            )


async def test_only_one_active_proposal_per_instrument(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _add(session, _proposal(status=ProposalStatus.READY))

    with pytest.raises(IntegrityError) as excinfo:
        async with clean_tables.transaction() as session:
            await _add(session, _proposal(status=ProposalStatus.NOTIFIED))
    assert "uq_trade_proposals_active_instrument" in str(excinfo.value)


async def test_a_new_proposal_is_allowed_once_the_previous_one_is_terminal(
    clean_tables: Database,
) -> None:
    async with clean_tables.transaction() as session:
        await _add(session, _proposal(status=ProposalStatus.EXECUTED))
    async with clean_tables.transaction() as session:
        await _add(session, _proposal(status=ProposalStatus.READY))


async def test_different_instruments_do_not_collide(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _add(session, _proposal(ticker="AAPL_US_EQ"))
        await _add(session, _proposal(ticker="VRT_US_EQ"))


async def test_approval_token_hash_is_unique(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        proposal = _proposal()
        await _add(session, proposal)
        token_hash = "1" * 64
        await _add(
            session,
            ApprovalAction(
                proposal_id=proposal.id,
                channel=ApprovalChannel.TELEGRAM,
                stage=ApprovalStage.APPROVE,
                opaque_token_hash=token_hash,
                user_identifier="123456789",
                expires_at=utcnow() + dt.timedelta(minutes=10),
            ),
        )
        proposal_id = proposal.id

    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            await _add(
                session,
                ApprovalAction(
                    proposal_id=proposal_id,
                    channel=ApprovalChannel.WEB,
                    stage=ApprovalStage.CONFIRM,
                    opaque_token_hash="1" * 64,
                    user_identifier="web-user",
                    expires_at=utcnow() + dt.timedelta(minutes=10),
                ),
            )


async def test_source_provider_item_id_is_deduplicated(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _add(
            session,
            Source(
                provider=SourceProvider.ALPACA,
                provider_item_id="benzinga-4242",
                content_hash="9" * 64,
                headline="Example",
            ),
        )

    with pytest.raises(IntegrityError) as excinfo:
        async with clean_tables.transaction() as session:
            await _add(
                session,
                Source(
                    provider=SourceProvider.ALPACA,
                    provider_item_id="benzinga-4242",
                    content_hash="8" * 64,
                    headline="Example, re-delivered",
                ),
            )
    assert "uq_sources_provider_item" in str(excinfo.value)


async def test_sources_without_provider_id_are_not_constrained(
    clean_tables: Database,
) -> None:
    """The partial index must not collapse every NULL-id source into one row."""
    async with clean_tables.transaction() as session:
        for index in range(3):
            await _add(
                session,
                Source(
                    provider=SourceProvider.FIRECRAWL,
                    provider_item_id=None,
                    content_hash=f"{index}" * 64,
                ),
            )


async def test_decimal_precision_survives_a_round_trip(clean_tables: Database) -> None:
    """Financial values must not be degraded to binary floating point."""
    quantity = Decimal("0.1234567891")
    price = Decimal("1234.56789012")
    async with clean_tables.transaction() as session:
        proposal = _proposal()
        proposal.proposed_quantity = quantity
        proposal.reference_price = price
        await _add(session, proposal)
        proposal_id = proposal.id

    async with clean_tables.session() as session:
        loaded = await session.get(TradeProposal, proposal_id)
        assert loaded is not None
        assert loaded.proposed_quantity == quantity
        assert loaded.reference_price == price
        assert isinstance(loaded.estimated_notional, Decimal)


async def test_timestamps_are_timezone_aware(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        proposal = _proposal()
        await _add(session, proposal)
        proposal_id = proposal.id

    async with clean_tables.session() as session:
        loaded = await session.get(TradeProposal, proposal_id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at.utcoffset() == dt.timedelta(0)


async def test_job_dedupe_key_prevents_duplicate_scheduling(
    clean_tables: Database,
) -> None:
    async with clean_tables.transaction() as session:
        await _add(session, Job(job_type="SEC_REFRESH", dedupe_key="sec:cik:0000320193"))

    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            await _add(session, Job(job_type="SEC_REFRESH", dedupe_key="sec:cik:0000320193"))

    # Once the first job is finished the same work may be scheduled again.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Job)
            .where(Job.dedupe_key == "sec:cik:0000320193")
            .values(status=JobStatus.SUCCEEDED)
        )
    async with clean_tables.transaction() as session:
        await _add(session, Job(job_type="SEC_REFRESH", dedupe_key="sec:cik:0000320193"))


async def test_skip_locked_claiming_hands_each_worker_a_distinct_job(
    clean_tables: Database,
) -> None:
    """The PostgreSQL-backed queue must not hand the same job to two workers."""
    async with clean_tables.transaction() as session:
        for index in range(2):
            await _add(session, Job(job_type="CLASSIFY_EVENT", payload={"n": index}))

    claim = (
        sa.select(Job.id)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= sa.func.now())
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    started = asyncio.Event()

    async def worker(hold: bool) -> uuid.UUID | None:
        async with clean_tables.transaction() as session:
            claimed = (await session.execute(claim)).scalar_one_or_none()
            if hold:
                started.set()
                # Hold the row lock while the second worker makes its attempt.
                await asyncio.sleep(0.3)
            else:
                await started.wait()
            return claimed

    first, second = await asyncio.gather(worker(True), worker(False))
    assert first is not None
    assert second is not None
    assert first != second


async def test_quantity_must_be_positive(clean_tables: Database) -> None:
    """Direction is carried by ``side``; a negative quantity is a bug, not a sell."""
    with pytest.raises(IntegrityError) as excinfo:
        async with clean_tables.transaction() as session:
            proposal = _proposal()
            proposal.proposed_quantity = Decimal("-1")
            await _add(session, proposal)
    assert "quantity_positive" in str(excinfo.value)
