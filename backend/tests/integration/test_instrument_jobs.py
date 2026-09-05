"""Job wiring for Phase 4.

The pipeline this file protects:

    classified event -> affected-company hint -> RESOLVE_CANDIDATES
        -> verified company / verified instrument, or a visible unresolved state

with the queue guarantees that make it survive restarts and redelivery: one
pending job per event, one pending refresh per broker, and a handler that treats
NOT_FOUND and AMBIGUOUS as answers rather than failures.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    EventStatus,
    ImpactDirection,
    JobStatus,
    JobType,
    ResolutionStatus,
)
from stockbrain.instruments.service import ResolutionService
from stockbrain.jobs.handlers import handle_resolve_candidates, register_ingestion_handlers
from stockbrain.jobs.queue import JobQueue
from stockbrain.jobs.registry import HandlerContext, JobRegistry

pytestmark = pytest.mark.integration


class StubServices:
    def __init__(self, database: Database) -> None:
        self.resolution = ResolutionService(database)
        self.queue = JobQueue()


def _context(database: Database, event_id: uuid.UUID) -> HandlerContext:
    return HandlerContext(
        job_id=uuid.uuid4(),
        job_type=JobType.RESOLVE_CANDIDATES.value,
        payload={"event_id": str(event_id)},
        attempt=1,
        max_attempts=3,
        database=database,
        services=StubServices(database),
    )


async def _seed(database: Database, *, hint: str, ticker: str | None) -> uuid.UUID:
    async with database.transaction() as session:
        event = Event(
            title=f"news {hint}",
            status=EventStatus.CLASSIFIED,
            first_seen_at=utcnow(),
            title_hash="a" * 64,
        )
        session.add(event)
        await session.flush()
        session.add(
            EventCompanyImpact(
                event_id=event.id,
                company_name_hint=hint,
                company_key=hint.lower(),
                ticker_hint=ticker,
                direction=ImpactDirection.POSITIVE,
                impact_path="direct",
                materiality_score=0.7,
                confidence=0.8,
            )
        )
        return event.id


async def _instrument(database: Database, **kwargs: Any) -> None:
    async with database.transaction() as session:
        session.add(
            BrokerInstrument(
                broker=Broker.TRADING212,
                is_active=True,
                last_refreshed_at=utcnow(),
                last_seen_at=utcnow(),
                **kwargs,
            )
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_resolve_candidates_is_registered_even_without_broker_credentials() -> None:
    """Resolution reads metadata already in the database.

    Leaving it unregistered would strand the pipeline; registering it means an
    unsynced deployment reports NOT_FOUND, which is the honest answer.
    """
    registry = JobRegistry()
    register_ingestion_handlers(
        registry, classifier_available=False, instrument_sync_available=False
    )
    assert JobType.RESOLVE_CANDIDATES.value in registry.known_types()
    assert JobType.INSTRUMENT_REFRESH.value not in registry.known_types()


def test_instrument_refresh_is_registered_only_when_it_can_run() -> None:
    """A handler guaranteed to fail would fill the queue with doomed jobs."""
    registry = JobRegistry()
    register_ingestion_handlers(registry, classifier_available=True, instrument_sync_available=True)
    assert JobType.INSTRUMENT_REFRESH.value in registry.known_types()
    assert JobType.CLASSIFY_EVENT.value in registry.known_types()


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
async def test_the_handler_resolves_and_persists(clean_tables: Database) -> None:
    await _instrument(
        clean_tables,
        broker_ticker="AAPL_US_EQ",
        name="Apple Inc.",
        short_name="AAPL",
        isin="US0378331005",
        currency="USD",
        instrument_type="STOCK",
        exchange="NASDAQ",
        market_symbol="AAPL",
        market_code="US",
        name_key="apple",
    )
    event_id = await _seed(clean_tables, hint="Apple Inc.", ticker="AAPL")
    await handle_resolve_candidates(_context(clean_tables, event_id))

    async with clean_tables.session() as session:
        impact = (
            await session.execute(
                sa.select(EventCompanyImpact).where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
    assert impact.resolution_status is ResolutionStatus.RESOLVED
    assert impact.broker_instrument_id is not None


async def test_an_unresolvable_hint_does_not_fail_the_job(clean_tables: Database) -> None:
    """NOT_FOUND is an answer. Failing the job would retry it three times and
    then bury the fact in a dead job row instead of on the impact."""
    await _instrument(
        clean_tables,
        broker_ticker="AAPL_US_EQ",
        name="Apple Inc.",
        short_name="AAPL",
        isin="US0378331005",
        instrument_type="STOCK",
        market_symbol="AAPL",
        name_key="apple",
    )
    event_id = await _seed(clean_tables, hint="Imaginary Corp", ticker="ZZZZ")
    await handle_resolve_candidates(_context(clean_tables, event_id))

    async with clean_tables.session() as session:
        impact = (
            await session.execute(
                sa.select(EventCompanyImpact).where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
    assert impact.resolution_status is ResolutionStatus.NOT_FOUND


async def test_running_the_handler_twice_is_a_no_op(clean_tables: Database) -> None:
    await _instrument(
        clean_tables,
        broker_ticker="AAPL_US_EQ",
        name="Apple Inc.",
        short_name="AAPL",
        isin="US0378331005",
        instrument_type="STOCK",
        market_symbol="AAPL",
        name_key="apple",
    )
    event_id = await _seed(clean_tables, hint="Apple Inc.", ticker="AAPL")
    await handle_resolve_candidates(_context(clean_tables, event_id))
    await handle_resolve_candidates(_context(clean_tables, event_id))

    async with clean_tables.session() as session:
        impacts = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(EventCompanyImpact)
                .where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
    assert impacts == 1


# ---------------------------------------------------------------------------
# Queue dedupe
# ---------------------------------------------------------------------------
async def test_one_pending_resolve_job_per_event(clean_tables: Database) -> None:
    """``uq_jobs_dedupe_key_active`` makes this a database guarantee."""
    queue = JobQueue()
    event_id = uuid.uuid4()
    async with clean_tables.transaction() as session:
        first = await queue.enqueue(
            session,
            JobType.RESOLVE_CANDIDATES,
            payload={"event_id": str(event_id)},
            dedupe_key=f"resolve:{event_id}",
        )
        second = await queue.enqueue(
            session,
            JobType.RESOLVE_CANDIDATES,
            payload={"event_id": str(event_id)},
            dedupe_key=f"resolve:{event_id}",
        )
    assert first is not None
    assert second is None, "a backlog of identical resolutions must not accumulate"


async def test_one_pending_instrument_refresh_per_broker(clean_tables: Database) -> None:
    """The endpoint allows one request per 50 seconds; a backlog would only wait."""
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        first = await queue.enqueue(
            session,
            JobType.INSTRUMENT_REFRESH,
            payload={"broker": Broker.TRADING212.value},
            dedupe_key=f"instruments:{Broker.TRADING212.value}",
        )
        second = await queue.enqueue(
            session,
            JobType.INSTRUMENT_REFRESH,
            payload={"broker": Broker.TRADING212.value},
            dedupe_key=f"instruments:{Broker.TRADING212.value}",
        )
    assert first is not None
    assert second is None


async def test_a_completed_refresh_frees_the_dedupe_key(clean_tables: Database) -> None:
    """Restart-safety: the partial index covers active jobs only, so a finished
    refresh does not block the next scheduled one forever."""
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(
            session,
            JobType.INSTRUMENT_REFRESH,
            payload={},
            dedupe_key="instruments:TRADING212",
        )
        assert job_id is not None
        await session.execute(
            sa.update(Job).where(Job.id == job_id).values(status=JobStatus.SUCCEEDED)
        )
    async with clean_tables.transaction() as session:
        again = await queue.enqueue(
            session,
            JobType.INSTRUMENT_REFRESH,
            payload={},
            dedupe_key="instruments:TRADING212",
        )
    assert again is not None
