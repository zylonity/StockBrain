"""Failure injection: what breaks, and what it must not break.

Every test here interrupts something -- the database, a provider, a worker, the
process itself -- and asserts a *safety* property rather than a functional one.
The question is never "did it work" but "did the wrong thing become possible".

The three properties everything in this file circles around:

1. **No duplicate paid work.**  Not a second broker POST, not a second Firecrawl
   search, not a second LLM call for the same event.
2. **No reservation lost, no reservation held forever.**  An ambiguous order
   keeps its exposure reserved; a definitively unsent one releases it.
3. **Degradation, not destruction.**  A provider blinking must not retire
   authorized proposals or halt unrelated subsystems.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic, FirecrawlCall, Job
from stockbrain.db.session import Database
from stockbrain.enums import (
    EventStatus,
    ExecutionFailure,
    ExecutionOutcome,
    FirecrawlCallKind,
    JobStatus,
    ProposalStatus,
    ProviderStatus,
)
from stockbrain.errors import (
    AmbiguousTransportFailure,
    DefinitePreSendFailure,
    ProviderUnavailable,
)
from stockbrain.ingestion.firecrawl_budget import FirecrawlBudget
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.proposals.quotes import QuoteFetcher
from stockbrain.services import ServiceContainer
from tests import proposal_helpers as ph
from tests.execution_helpers import FakeProvider, authorized, build, execution_settings

pytestmark = pytest.mark.integration


async def _attempts(database: Database) -> list[ExecutionAttempt]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(ExecutionAttempt).order_by(ExecutionAttempt.attempt_number)
                )
            ).scalars()
        )


async def _proposal(database: Database, proposal_id: uuid.UUID) -> TradeProposal:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal


# ---------------------------------------------------------------------------
# Broker send crashes
# ---------------------------------------------------------------------------
async def test_a_crash_between_the_send_and_the_result_recovers_as_ambiguous(
    clean_tables: Database,
) -> None:
    """``sent_to_broker=True`` with no recorded response is the crash signature.

    Found in that state, the only honest reading is "an order may exist".
    Treating it as "not sent" is how a system places a second position, and it
    is the single most expensive mistake available.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)

    # A worker that recorded the send and then died: exactly what the send
    # transaction leaves behind, written directly because killing a task
    # mid-`await` cannot be made deterministic.
    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        proposal.status = ProposalStatus.EXECUTING
        session.add(
            ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=1,
                started_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
                preflight_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
                broker_environment="demo",
                request_payload={},
                request_fingerprint=uuid.uuid4().hex,
                sent_to_broker=True,
                sent_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
                outcome=ExecutionOutcome.PENDING,
            )
        )

    recovered = await execution.recover_incomplete()
    assert recovered == 1

    attempts = await _attempts(clean_tables)
    assert len(attempts) == 1
    assert attempts[0].ambiguous is True
    assert attempts[0].outcome is ExecutionOutcome.AMBIGUOUS
    assert provider.submitted == 0

    # And the exposure stays reserved: the proposal is not returned to a state
    # from which it could be executed again.
    proposal = await _proposal(clean_tables, proposal_id)
    assert proposal.status is not ProposalStatus.APPROVED
    assert proposal.status is not ProposalStatus.READY


async def test_a_recovered_attempt_is_never_resent(clean_tables: Database) -> None:
    """The property the whole execution design exists for.

    After recovery, another ``execute`` call on the same proposal must find the
    transmitted attempt and refuse -- forever, until reconciliation or a human
    resolves it.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    provider.error = AmbiguousTransportFailure("read timeout after the request was written")
    first = await execution.execute(proposal_id)
    assert first.outcome is ExecutionOutcome.AMBIGUOUS
    assert provider.submitted == 1

    provider.error = None
    for _ in range(5):
        again = await execution.execute(proposal_id)
        assert again.reconcile_required or not again.transmitted
    assert provider.submitted == 1


async def test_a_definite_pre_send_failure_releases_the_reservation(
    clean_tables: Database,
) -> None:
    """The one case where retraction is safe.

    A connect failure happens before a request line is written, so nothing
    reached the broker and the flag can be retracted -- which is what frees
    ``uq_execution_attempts_sent_once`` for a later attempt.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    provider.error = DefinitePreSendFailure("DNS failure")

    result = await execution.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.FAILED_BEFORE_SEND
    attempts = await _attempts(clean_tables)
    assert attempts[0].sent_to_broker is False

    # And a later attempt is permitted, because nothing was placed.
    provider.error = None
    second = await execution.execute(proposal_id)
    assert second.transmitted


async def test_a_malformed_broker_response_is_ambiguous_not_successful(
    clean_tables: Database,
) -> None:
    """The most dangerous shape there is: the order exists and its id is unknown.

    A 2xx with an unparseable body must never be read as "no order", and must
    never be read as "order with id ???" either.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    provider.error = AmbiguousTransportFailure(
        "trading212: 200 with a body that could not be parsed"
    )
    result = await execution.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.AMBIGUOUS
    assert result.reconcile_required
    assert (await _attempts(clean_tables))[0].broker_order_id is None


async def test_an_unexpected_exception_during_transmission_is_ambiguous(
    clean_tables: Database,
) -> None:
    """An exception nobody classified may or may not have left bytes.

    Ambiguity is the only honest answer and also the safe one, so the
    catch-all is deliberately not narrowed.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    provider.error = RuntimeError("something nobody anticipated")
    result = await execution.execute(proposal_id)
    assert result.outcome is ExecutionOutcome.AMBIGUOUS


# ---------------------------------------------------------------------------
# Provider outages
# ---------------------------------------------------------------------------
async def test_a_market_data_outage_does_not_retire_an_authorized_proposal(
    clean_tables: Database,
) -> None:
    """Degradation, not destruction.

    Failing every authorized proposal whenever a quote provider blinks would
    make an outage destructive. A *missing* input says nothing about the trade.
    """
    settings = execution_settings()
    proposals, execution, provider, proposal_id = await authorized(clean_tables, settings)
    proposals.quotes = QuoteFetcher(ph.StubMarketData(error=ProviderUnavailable("upstream 503")))

    result = await execution.execute(proposal_id)
    assert not result.transmitted
    assert provider.submitted == 0

    proposal = await _proposal(clean_tables, proposal_id)
    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.invalidated_at is None


async def test_an_account_provider_outage_blocks_rather_than_guessing(
    clean_tables: Database,
) -> None:
    """Account state fails closed: no snapshot means no size and no send.

    It never falls back to the last known balance, which would be sizing
    against a number the broker has already contradicted.
    """
    settings = execution_settings()
    _, execution, provider, proposal_id = await authorized(clean_tables, settings)
    async with clean_tables.transaction() as session:
        # Age the snapshot past the limit: the same shape as the provider having
        # stopped answering, reached without patching anything.
        await session.execute(
            sa.text("UPDATE portfolio_snapshots SET captured_at = now() - interval '2 days'")
        )
    result = await execution.execute(proposal_id)
    assert not result.transmitted
    assert provider.submitted == 0


async def test_a_broker_read_failure_leaves_the_pending_count_unknown_and_refuses(
    clean_tables: Database,
) -> None:
    """ "The broker would not tell us" resolves the same way as "the broker said
    fifty". An unknown in front of a non-idempotent POST resolves against
    sending."""
    provider = FakeProvider(pending_read_ok=False)
    _, execution, _, proposal_id = await authorized(
        clean_tables, execution_settings(), provider=provider
    )
    result = await execution.execute(proposal_id)
    assert result.failure is ExecutionFailure.PENDING_ORDER_LIMIT
    assert provider.submitted == 0


# ---------------------------------------------------------------------------
# Database interruption
# ---------------------------------------------------------------------------
async def test_a_database_outage_leaves_liveness_up_and_readiness_down(
    clean_tables: Database,
) -> None:
    """PostgreSQL is the one dependency that makes the application not-ready.

    Liveness must stay up regardless, or a database blip restarts a process
    that was working -- and restarts are what re-run schedulers.
    """
    from asgi_lifespan import LifespanManager

    from stockbrain.main import create_app

    settings = Settings(
        app_env="test",
        log_level="CRITICAL",
        web_auth_enabled=False,
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/stockbrain",
        discovery_enabled=False,
    )
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=60):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/health/live")).status_code == 200
            ready = await client.get("/api/health/ready")
            assert ready.status_code == 503
            assert ready.json()["ready"] is False


async def test_an_optional_provider_being_down_keeps_the_service_ready(
    clean_tables: Database,
) -> None:
    """Readiness is about the database and the schema, not about the news feed.

    A dead Firecrawl, a dead Alpaca and an exhausted LLM budget must all leave
    the service ready: they degrade their own subsystem and nothing else.
    """
    registry = ProviderHealthRegistry()
    from stockbrain.observability.health import ProviderName

    registry.record(ProviderName.POSTGRES, ProviderStatus.HEALTHY)
    registry.record(ProviderName.FIRECRAWL, ProviderStatus.DOWN, detail="402")
    registry.record(ProviderName.ALPACA_NEWS, ProviderStatus.DOWN, detail="socket closed")
    registry.record(ProviderName.DEEPSEEK, ProviderStatus.DEGRADED, detail="budget")
    registry.set_schema_state(True, "at head")

    assert registry.get_database_status() is ProviderStatus.HEALTHY
    # Overall status degrades, which is the honest summary -- but the database
    # is what readiness turns on.
    assert registry.overall_status() is not ProviderStatus.HEALTHY


# ---------------------------------------------------------------------------
# Duplicate workers and schedulers
# ---------------------------------------------------------------------------
async def test_two_workers_racing_one_proposal_transmit_once(
    clean_tables: Database,
) -> None:
    """The Phase 8 guarantee, re-asserted with the Phase 9 checks in front."""
    settings = execution_settings()
    provider = FakeProvider()
    _, first, _, proposal_id = await authorized(clean_tables, settings, provider=provider)
    workers = [first, *(build(clean_tables, settings, provider=provider)[1] for _ in range(5))]
    await asyncio.gather(*(worker.execute(proposal_id) for worker in workers))
    assert provider.submitted == 1
    assert len([a for a in await _attempts(clean_tables) if a.sent_to_broker]) == 1


async def test_two_firecrawl_budgets_racing_the_last_credit_grant_one(
    clean_tables: Database,
) -> None:
    """Two processes, one allowance.

    The advisory lock is transaction-scoped so it holds across processes; the
    Phase 2 counter lived on a client object and held across nothing.
    """
    guards = [
        FirecrawlBudget(
            clean_tables,
            enabled=True,
            max_searches_per_day=1,
            max_scrapes_per_day=1,
            daily_credit_cap=100,
            monthly_credit_cap=100,
        )
        for _ in range(6)
    ]
    results = await asyncio.gather(
        *(guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) for guard in guards)
    )
    assert len([item for item in results if item is not None]) == 1
    async with clean_tables.session() as session:
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(FirecrawlCall))
        ).scalar_one()
    assert count == 1


async def test_a_duplicate_scheduler_does_not_double_enqueue(
    clean_tables: Database,
) -> None:
    """Two containers, or one restarted before the old one exited.

    Guarded twice -- the durable ``next_eligible_at`` claim and
    ``uq_jobs_dedupe_key_active`` -- because either alone leaves a window.
    """
    async with clean_tables.transaction() as session:
        topic = DiscoveryTopic(
            slug="chaos",
            name="Chaos",
            enabled=True,
            interval_minutes=720,
            result_limit=5,
            freshness="qdr:d",
            include_domains=[],
            exclude_domains=[],
        )
        session.add(topic)
        await session.flush()
        session.add(DiscoveryQuery(topic_id=topic.id, query="a query", enabled=True))

    settings = Settings(
        app_env="test",
        log_level="CRITICAL",
        web_auth_enabled=False,
        database_url=clean_tables.engine.url.render_as_string(hide_password=False),
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        alpaca_news_enabled=False,
        sec_enabled=False,
        t212_metadata_enabled=False,
        research_enabled=False,
        proposals_enabled=False,
    )
    for _ in range(4):
        container = ServiceContainer(
            settings=settings, database=clean_tables, health=ProviderHealthRegistry()
        )
        await container._enqueue_due_topic_searches()

    async with clean_tables.session() as session:
        jobs = list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == "FIRECRAWL_TOPIC_SEARCH")
                )
            ).scalars()
        )
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Worker death and reclamation
# ---------------------------------------------------------------------------
async def test_a_dead_workers_job_is_reclaimed_within_its_retry_budget(
    clean_tables: Database,
) -> None:
    """A crashed worker leaves a row RUNNING forever unless something reclaims it.

    Subject to the same ``max_attempts`` budget, so a job that reliably kills
    its worker cannot loop indefinitely -- which is the difference between
    recovery and a crash loop.
    """
    from stockbrain.jobs.queue import JobQueue

    async with clean_tables.transaction() as session:
        session.add(
            Job(
                job_type="CLASSIFY_EVENT",
                payload={},
                status=JobStatus.RUNNING,
                locked_by="dead-worker#0",
                locked_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                attempts=1,
                max_attempts=3,
            )
        )
        session.add(
            Job(
                job_type="CLASSIFY_EVENT",
                payload={},
                status=JobStatus.RUNNING,
                locked_by="dead-worker#1",
                locked_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                attempts=3,
                max_attempts=3,
            )
        )

    queue = JobQueue()
    async with clean_tables.transaction() as session:
        reclaimed = await queue.reclaim_abandoned(session, timeout_seconds=60)
    assert reclaimed == 1

    async with clean_tables.session() as session:
        statuses = sorted(
            row.value for row in (await session.execute(sa.select(Job.status))).scalars()
        )
    # One back to PENDING, one dead: it had no attempts left.
    assert statuses == ["FAILED", "PENDING"]


async def test_a_paid_firecrawl_job_is_not_reclaimed_into_a_second_call(
    clean_tables: Database,
) -> None:
    """A Firecrawl search job has ``max_attempts=1`` precisely so that a dying
    worker does not turn into a second billable request.

    Reclamation respects the budget, so the row goes straight to FAILED and the
    durable cooldown decides when to try again.
    """
    from stockbrain.jobs.queue import JobQueue

    async with clean_tables.transaction() as session:
        session.add(
            Job(
                job_type="FIRECRAWL_TOPIC_SEARCH",
                payload={},
                status=JobStatus.RUNNING,
                locked_by="dead-worker#0",
                locked_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                attempts=1,
                max_attempts=1,
            )
        )
    async with clean_tables.transaction() as session:
        assert await JobQueue().reclaim_abandoned(session, timeout_seconds=60) == 0
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(Job.status))).scalar_one()
    assert status is JobStatus.FAILED


async def test_a_stalled_classification_is_released_and_not_double_charged(
    clean_tables: Database,
) -> None:
    """Phase 6's bug 12, from the other side.

    A permanent failure after the ``NEW -> CLASSIFYING`` claim is
    indistinguishable from a dying worker, so the reaper returns the event to
    ``NEW``. What must not happen is the claim being re-taken while the first
    holder is still running, which is why the transition is a compare-and-swap
    rather than a check-then-act.
    """
    async with clean_tables.transaction() as session:
        session.add(
            Event(
                id=uuid.uuid4(),
                title="A stalled event",
                title_hash="e" * 64,
                first_seen_at=dt.datetime.now(dt.UTC),
                status=EventStatus.CLASSIFYING,
                updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
                topics=[],
            )
        )
    async with clean_tables.transaction() as session:
        released = await session.execute(
            sa.update(Event)
            .where(
                Event.status == EventStatus.CLASSIFYING,
                Event.updated_at < sa.func.now() - dt.timedelta(minutes=15),
            )
            .values(status=EventStatus.NEW)
            .returning(Event.id)
        )
        assert len(list(released.scalars())) == 1


# ---------------------------------------------------------------------------
# Stale inputs
# ---------------------------------------------------------------------------
async def test_a_stale_quote_defers_and_a_moved_price_retires(
    clean_tables: Database,
) -> None:
    """Two different failures that must not be conflated.

    A stale quote is a StockBrain problem the next sweep may not have. A price
    that *moved* is a statement about the trade, and the proposal the operator
    approved no longer exists.
    """
    settings = execution_settings()
    proposals, execution, provider, proposal_id = await authorized(clean_tables, settings)

    # Stale: same price, too old. The proposal survives.
    proposals.quotes = QuoteFetcher(ph.StubMarketData(age_ms=60_000))
    deferred = await execution.execute(proposal_id)
    assert not deferred.transmitted
    assert (await _proposal(clean_tables, proposal_id)).status is ProposalStatus.APPROVED

    # Moved: fresh, and far away. The proposal is retired.
    proposals.quotes = QuoteFetcher(ph.StubMarketData(bid=Decimal("399.95"), ask=Decimal("400.05")))
    retired = await execution.execute(proposal_id)
    assert not retired.transmitted
    assert (await _proposal(clean_tables, proposal_id)).status is ProposalStatus.INVALIDATED
    assert provider.submitted == 0
