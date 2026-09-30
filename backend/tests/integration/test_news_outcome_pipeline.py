"""Cache miss -> background backfill -> one reassessment; catalyst deadline reviews.

Every step runs against the real migrations and the real research service with
a deterministic engine. Providers are fixtures; nothing reaches a network.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa

from stockbrain.db.models.companies import Company
from stockbrain.db.models.news_catalyst import NewsCatalystDeadline
from stockbrain.db.models.news_history import NewsHistoryCandle
from stockbrain.db.models.portfolio import BrokerOrder, Position
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import (
    BarTimeframe,
    Broker,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    SourceProvider,
    ThesisAction,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.intelligence.news_catalyst_monitor import CatalystMonitor
from stockbrain.intelligence.news_outcome_research import NewsOutcomeResearchProvider
from stockbrain.intelligence.research import ResearchPacket
from stockbrain.intelligence.research_service import (
    CATALYST_REVIEW_TRIGGER,
    NEWS_CACHE_POSITION_TRIGGER,
    NEWS_CACHE_REASSESSMENT_TRIGGER,
    NEWS_OUTCOME_BACKFILL_JOB,
    ResearchService,
    advisory_triggers,
)
from stockbrain.jobs.handlers import run_news_outcome_backfill
from stockbrain.jobs.registry import HandlerContext
from stockbrain.market_data.base import Bar
from stockbrain.proposals.exits import ExitSweepService
from tests import proposal_helpers as ph
from tests.integration.test_research import Engine, seed, service

pytestmark = pytest.mark.integration


class _News:
    name = "fixture_news"

    def __init__(self) -> None:
        self.calls = 0

    async def backfill(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
        max_pages: int | None = None,
    ) -> list[RawSourceDocument]:
        self.calls += 1
        return [
            RawSourceDocument(
                provider=SourceProvider.ALPACA,
                provider_item_id=f"hist-{start.date()}",
                headline="Apple earlier capacity story",
                published_at=start + dt.timedelta(days=1, hours=15),
                symbols=["AAPL"],
            )
        ]


class _Bars:
    name = "fixture_bars"
    last_bars_requests = 1
    last_bars_truncated = False

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> list[Bar]:
        price = Decimal("100") if symbol == "AAPL" else Decimal("500")
        return [
            Bar(
                symbol=symbol,
                timestamp=start + dt.timedelta(minutes=minute),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100,
                feed="sip",
            )
            for minute in range(3)
        ]


async def _classified(db: Database) -> ResearchPacket:
    value = await seed(db)
    async with db.transaction() as session:
        event = await session.get(Event, value.event_id)
        company = await session.get(Company, value.company.company_id)
        assert event is not None and company is not None
        event.event_type = "CAPACITY"
        event.classified_at = value.event_time
        company.primary_symbol = "AAPL"
    return value


def _research(db: Database, engine: Engine) -> ResearchService:
    research = service(db, engine)
    research.outcomes = NewsOutcomeResearchProvider(db)
    return research


async def _first_run(db: Database, research: ResearchService, value: ResearchPacket) -> uuid.UUID:
    async with db.transaction() as session:
        run_id = await research.request(session, value.impact_id, as_of=value.as_of)
    await research.run(run_id)
    return run_id


async def _jobs(db: Database) -> list[Job]:
    async with db.session() as session:
        return list(
            (await session.scalars(sa.select(Job).where(Job.job_type == NEWS_OUTCOME_BACKFILL_JOB)))
            .unique()
            .all()
        )


def _context(db: Database, research: ResearchService, job: Job) -> HandlerContext:
    return HandlerContext(
        job_id=job.id,
        job_type=job.job_type,
        payload=dict(job.payload),
        attempt=1,
        max_attempts=2,
        database=db,
        services=SimpleNamespace(research=research),
    )


async def test_a_cache_miss_enqueues_one_bounded_backfill_without_blocking(
    clean_tables: Database,
) -> None:
    value = await _classified(clean_tables)
    engine = Engine()
    research = _research(clean_tables, engine)
    first = await _first_run(clean_tables, research, value)
    # The research run itself completed; the backfill is only queued.
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, first)
        assert row is not None and row.status.value == "SUCCEEDED"
    assert engine.calls == 1

    # A second live story on the same company/window joins the queued job.
    async with clean_tables.transaction() as session:
        second = await research.request(
            session, value.impact_id, as_of=value.as_of, rerun_id=uuid.uuid4()
        )
    await research.run(second)

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 1
    payload = jobs[0].payload
    assert payload["symbol"] == "AAPL"
    assert payload["affected_run_ids"] == [str(first), str(second)]
    start = dt.datetime.fromisoformat(payload["start"])
    end = dt.datetime.fromisoformat(payload["end"])
    assert end - start <= dt.timedelta(days=14)
    assert end <= value.event_time


async def test_a_completed_backfill_requests_one_reassessment_and_never_loops(
    clean_tables: Database,
) -> None:
    value = await _classified(clean_tables)
    engine = Engine()
    research = _research(clean_tables, engine)
    first = await _first_run(clean_tables, research, value)
    [job] = await _jobs(clean_tables)

    await run_news_outcome_backfill(
        _context(clean_tables, research, job), news=_News(), bars=_Bars()
    )
    # A redelivered job produces the same versioned rerun, not a second one.
    await run_news_outcome_backfill(
        _context(clean_tables, research, job), news=_News(), bars=_Bars()
    )

    async with clean_tables.session() as session:
        reruns = (
            (
                await session.scalars(
                    sa.select(ResearchRun).where(
                        ResearchRun.trigger == NEWS_CACHE_REASSESSMENT_TRIGGER
                    )
                )
            )
            .unique()
            .all()
        )
        original = await session.get(ResearchRun, first)
    assert len(reruns) == 1
    assert original is not None and original.as_of is not None
    assert reruns[0].as_of is not None and reruns[0].as_of > original.as_of

    # Running the reassessment must not enqueue another backfill.
    await research.run(reruns[0].id)
    assert len(await _jobs(clean_tables)) == 1
    assert engine.packet is not None and engine.packet.previous_thesis_id is not None


async def test_a_filled_buy_gets_an_advisory_position_reassessment(
    clean_tables: Database,
) -> None:
    value = await _classified(clean_tables)
    research = _research(clean_tables, Engine())
    first = await _first_run(clean_tables, research, value)
    await _buy_world(clean_tables, value, first, entry=_ENTRY)
    [job] = await _jobs(clean_tables)

    await run_news_outcome_backfill(
        _context(clean_tables, research, job), news=_News(), bars=_Bars()
    )
    async with clean_tables.session() as session:
        triggers = (
            await session.scalars(sa.select(ResearchRun.trigger).where(ResearchRun.id != first))
        ).all()
    assert triggers == [NEWS_CACHE_POSITION_TRIGGER]
    assert NEWS_CACHE_POSITION_TRIGGER in advisory_triggers(True)
    assert NEWS_CACHE_POSITION_TRIGGER in advisory_triggers(False)


# 2026-09-08 is a Tuesday; 14:00 UTC is 10:00 in New York.
_ENTRY = dt.datetime(2026, 9, 8, 14, tzinfo=dt.UTC)
_EVIDENCE = {
    "threshold": "0.01",
    "sample_size": 12,
    "responders": 10,
    "nonresponses": 2,
    "buffer_sessions": 1,
    "deadline_sessions_after_entry": 2,
}


async def _buy_world(
    db: Database, value: ResearchPacket, run_id: uuid.UUID, *, entry: dt.datetime
) -> uuid.UUID:
    """A filled news-driven BUY from ``run_id``, with the position still held."""
    async with db.transaction() as session:
        run = await session.get(ResearchRun, run_id)
        thesis = await session.scalar(sa.select(Thesis).where(Thesis.research_run_id == run_id))
        assert run is not None and thesis is not None
        thesis.action = ThesisAction.BUY
        # Research completed before the simulated fill, as it would live.
        run.completed_at = value.as_of
        assert run.research_packet is not None
        packet = dict(run.research_packet)
        packet["market_context"] = [
            {
                "provider": "news_outcomes",
                "kind": "historical_news_candle_outcomes",
                "text": json.dumps({"catalyst_deadline_evidence": _EVIDENCE}),
            }
        ]
        run.research_packet = packet
        proposal = TradeProposal(
            id=uuid.uuid4(),
            broker=Broker.TRADING212,
            broker_ticker=value.company.broker_ticker,
            account_id="default",
            broker_environment="live",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            proposed_quantity=Decimal("10"),
            reference_price=Decimal("100"),
            reference_currency="USD",
            price_source=PriceSource.ALPACA_IEX,
            quote_timestamp=entry,
            quote_age_ms=500,
            estimated_notional=Decimal("1000"),
            account_currency="USD",
            event_id=value.event_id,
            company_id=value.company.company_id,
            broker_instrument_id=value.company.broker_instrument_id,
            thesis_id=thesis.id,
            research_run_id=run_id,
            executed_at=entry,
            status=ProposalStatus.EXECUTED,
            expires_at=entry + dt.timedelta(days=1),
            research_action=ThesisAction.BUY.value,
            research_confidence=0.7,
        )
        session.add(proposal)
        await session.flush()
        session.add(
            BrokerOrder(
                id=uuid.uuid4(),
                broker=Broker.TRADING212,
                broker_order_id="order-1",
                proposal_id=proposal.id,
                broker_ticker=value.company.broker_ticker,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("10"),
                filled_quantity=Decimal("10"),
                broker_status="FILLED",
                broker_environment="live",
                submitted_at=entry,
                last_synced_at=entry,
                is_terminal=True,
                raw={},
            )
        )
        session.add(
            Position(
                broker=Broker.TRADING212,
                broker_ticker=value.company.broker_ticker,
                company_id=value.company.company_id,
                quantity=Decimal("10"),
                last_synced_at=entry,
            )
        )
        return thesis.id


async def _session_bars(db: Database, day: dt.date, *, stock_close: str) -> None:
    """An entry-time bar on the first day and a closing bar every day."""
    async with db.transaction() as session:
        moments = [dt.datetime.combine(day, dt.time(19, 59), dt.UTC)]
        if day == _ENTRY.date():
            moments.insert(0, _ENTRY)
        for moment in moments:
            for symbol, close in (("AAPL", Decimal(stock_close)), ("SPY", Decimal("500"))):
                opening = Decimal("100") if symbol == "AAPL" else Decimal("500")
                session.add(
                    NewsHistoryCandle(
                        symbol=symbol,
                        timeframe="1Min",
                        provider="fixture",
                        feed="sip",
                        bar_start=moment,
                        observed_at=moment + dt.timedelta(minutes=1),
                        ingested_at=moment + dt.timedelta(minutes=2),
                        open=opening if moment == _ENTRY else close,
                        high=close,
                        low=close,
                        close=close,
                        volume=100,
                    )
                )


async def test_a_failed_catalyst_triggers_one_review_that_favours_an_exit(
    clean_tables: Database,
) -> None:
    value = await _classified(clean_tables)
    engine = Engine()
    research = _research(clean_tables, engine)
    first = await _first_run(clean_tables, research, value)
    origin_thesis = await _buy_world(clean_tables, value, first, entry=_ENTRY)
    monitor = CatalystMonitor(clean_tables, research)

    await monitor.tick(as_of=_ENTRY + dt.timedelta(hours=1))
    async with clean_tables.session() as session:
        item = await session.scalar(sa.select(NewsCatalystDeadline))
    assert item is not None
    assert item.market_symbol == "AAPL"
    assert item.entry_time_basis == "order_submitted_at_proxy"
    assert item.deadline_sessions_after_entry == 2
    assert item.expected_threshold == Decimal("0.01")
    assert item.measurement_status == "pending"

    for day in (8, 9, 10):
        await _session_bars(clean_tables, dt.date(2026, 9, day), stock_close="100.2")

    # One completed session after entry is not the deadline.
    await monitor.tick(as_of=dt.datetime(2026, 9, 9, 21, tzinfo=dt.UTC))
    async with clean_tables.session() as session:
        pending = await session.scalar(sa.select(NewsCatalystDeadline))
    assert pending is not None and pending.reassessment_requested_at is None

    deadline = dt.datetime(2026, 9, 10, 21, tzinfo=dt.UTC)
    await monitor.tick(as_of=deadline)
    await monitor.tick(as_of=deadline + dt.timedelta(hours=1))
    async with clean_tables.session() as session:
        item = await session.scalar(sa.select(NewsCatalystDeadline))
        reviews = (
            (
                await session.scalars(
                    sa.select(ResearchRun).where(ResearchRun.trigger == CATALYST_REVIEW_TRIGGER)
                )
            )
            .unique()
            .all()
        )
    assert item is not None
    assert item.measurement_status == "bar_proxy", item.measurement_status
    assert item.review_reason == "expected_reaction_failed"
    assert item.observed_market_relative_move == Decimal("0.002")
    assert len(reviews) == 1 and item.reassessment_run_id == reviews[0].id

    await research.run(reviews[0].id)
    assert engine.packet is not None and engine.packet.previous_thesis_id == origin_thesis
    [datum] = [d for d in engine.packet.market_context if d.provider == "news_outcomes"]
    review = json.loads(datum.text)["catalyst_review"]
    assert review["outcome"] == "expected_reaction_failed"
    assert review["deadline_sessions_after_entry"] == 2
    assert "favour SELL or REDUCE" in review["guidance"]


async def test_advisory_reviews_never_drive_the_exit_sweep(clean_tables: Database) -> None:
    value = await _classified(clean_tables)
    research = _research(clean_tables, Engine())
    first = await _first_run(clean_tables, research, value)
    origin = await _buy_world(clean_tables, value, first, entry=_ENTRY)
    async with clean_tables.transaction() as session:
        review_id = await research.request(
            session, value.impact_id, rerun_id=uuid.uuid4(), previous_thesis_id=origin
        )
        review = await session.get(ResearchRun, review_id)
        assert review is not None
        review.trigger = CATALYST_REVIEW_TRIGGER
    await research.run(review_id)
    async with clean_tables.transaction() as session:
        thesis = await session.scalar(sa.select(Thesis).where(Thesis.research_run_id == review_id))
        assert thesis is not None
        thesis.action = ThesisAction.SELL

    def sweep(enabled: bool) -> ExitSweepService:
        settings = SimpleNamespace(news_catalyst_exit_proposals_enabled=enabled)
        return ExitSweepService(
            clean_tables, cast(Any, settings), proposals=cast(Any, None), config=cast(Any, None)
        )

    async with clean_tables.session() as session:
        assert await sweep(False)._superseded(session, origin) is False
        assert await sweep(True)._superseded(session, origin) is True


async def test_advisory_reviews_never_become_proposals(clean_tables: Database) -> None:
    """Neither the one-minute backlog nor a direct generate call proposes them."""
    value = await _classified(clean_tables)
    research = _research(clean_tables, Engine())
    first = await _first_run(clean_tables, research, value)
    origin = await _buy_world(clean_tables, value, first, entry=_ENTRY)
    async with clean_tables.transaction() as session:
        review_id = await research.request(
            session, value.impact_id, rerun_id=uuid.uuid4(), previous_thesis_id=origin
        )
        review = await session.get(ResearchRun, review_id)
        assert review is not None
        review.trigger = CATALYST_REVIEW_TRIGGER
    await research.run(review_id)
    async with clean_tables.transaction() as session:
        thesis = await session.scalar(sa.select(Thesis).where(Thesis.research_run_id == review_id))
        assert thesis is not None
        thesis.action = ThesisAction.SELL

    proposals = ph.service(clean_tables)
    await proposals.enqueue_pending()
    async with clean_tables.session() as session:
        queued = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.payload["thesis_id"].astext == str(thesis.id))
        )
    assert queued == 0
    result = await proposals.generate(thesis.id)
    assert not result.created and result.reason == "advisory position review"
