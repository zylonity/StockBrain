"""What research is told about its own prior state (spec §7.1)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import Broker, ResearchStatus, ThesisAction, TimeHorizon
from stockbrain.intelligence.memory import MemoryService
from stockbrain.intelligence.research import PROMPT_VERSION, ResearchPacket
from stockbrain.intelligence.research_service import ResearchService
from tests.integration.test_research import Engine as FakeEngine
from tests.integration.test_research import seed, service
from tests.integration.test_thesis_memory_sweep import FakeBars, _settings

pytestmark = pytest.mark.integration


def _with_memory(db: Database, *, enabled: bool = True) -> ResearchService:
    research = service(db, FakeEngine())
    research.memory = MemoryService(db, _settings(), bars=FakeBars({}))
    research.memory_packet_enabled = enabled
    return research


async def _publish(
    db: Database,
    value: ResearchPacket,
    *,
    action: ThesisAction = ThesisAction.BUY,
    completed_at: dt.datetime,
    text: str = "Buy on the capacity story.",
) -> uuid.UUID:
    run_id, thesis_id = uuid.uuid4(), uuid.uuid4()
    async with db.transaction() as session:
        session.add(
            ResearchRun(
                id=run_id,
                event_id=value.event_id,
                impact_id=value.impact_id,
                company_id=value.company.company_id,
                broker_instrument_id=value.company.broker_instrument_id,
                status=ResearchStatus.SUCCEEDED,
                completed_at=completed_at,
                structured_decision={
                    "action": action.value,
                    "confidence": 0.72,
                    "horizon": "weeks",
                    "thesis": text,
                    "bull_case": "b",
                    "bear_case": "r",
                    "catalysts": [],
                    "risks": [],
                    "invalidation_conditions": ["Plant cancelled"],
                    "evidence_ids": [str(value.evidence[0].source_id)],
                },
            )
        )
        await session.flush()
        session.add(
            Thesis(
                id=thesis_id,
                research_run_id=run_id,
                action=action,
                confidence=0.72,
                time_horizon=TimeHorizon.WEEKS,
                summary=text,
                invalidation_conditions={"items": ["Plant cancelled"]},
            )
        )
    return thesis_id


async def _packet(db: Database, research: ResearchService, value: ResearchPacket) -> ResearchPacket:
    async with db.session() as session:
        return await research.packet(session, value.impact_id, value.as_of)


def test_the_prompt_version_moved_with_the_packet_shape() -> None:
    assert PROMPT_VERSION == "research-v3"


async def test_memory_is_absent_when_the_packet_flag_is_off(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    built = await _packet(clean_tables, _with_memory(clean_tables, enabled=False), value)
    assert built.memory is None


async def test_memory_is_present_but_empty_when_nothing_is_known(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.standing_thesis is None
    assert built.memory.position is None
    assert built.memory.company_record is None
    assert built.memory.event_type_record == ()


async def test_the_latest_thesis_within_the_age_window_is_the_standing_one(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    old = await _publish(
        clean_tables, value, completed_at=value.as_of - dt.timedelta(days=3), text="old"
    )
    newest = await _publish(
        clean_tables, value, completed_at=value.as_of - dt.timedelta(hours=2), text="newest"
    )
    await _publish(
        clean_tables, value, completed_at=value.as_of + dt.timedelta(hours=1), text="future"
    )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    standing = built.memory.standing_thesis
    assert standing is not None
    assert standing.thesis_id == newest
    assert standing.thesis_id != old
    assert standing.thesis == "newest"
    assert standing.invalidation_conditions == ("Plant cancelled",)
    assert 1.9 < standing.age_hours < 2.1
    # The chain is never linked automatically (spec §7.1).
    assert built.previous_thesis_id is None
    assert built.previous_thesis is None


async def test_a_thesis_older_than_the_window_is_not_standing(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    await _publish(clean_tables, value, completed_at=value.as_of - dt.timedelta(days=15))
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.standing_thesis is None


async def test_the_live_position_is_reported_when_synced_before_as_of(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=Broker.TRADING212,
                account_id="4242",
                broker_ticker=value.company.broker_ticker,
                quantity=Decimal("3"),
                quantity_available=Decimal("3"),
                average_price=Decimal("100"),
                current_price=Decimal("110"),
                currency="USD",
                initial_fill_date=value.as_of - dt.timedelta(days=1),
                last_synced_at=value.as_of - dt.timedelta(minutes=5),
            )
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    position = built.memory.position
    assert position is not None
    assert position.quantity == Decimal("3")
    assert position.unrealised_pct == Decimal("0.1")


async def test_a_position_synced_after_as_of_is_unknown_to_a_historical_run(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=Broker.TRADING212,
                account_id="4242",
                broker_ticker=value.company.broker_ticker,
                quantity=Decimal("3"),
                currency="USD",
                last_synced_at=value.as_of + dt.timedelta(minutes=5),
            )
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.position is None


async def _graded_outcome(
    db: Database,
    value: ResearchPacket,
    *,
    action: ThesisAction,
    event_type: str,
    correct: bool,
    alpha: str,
    graded_at: dt.datetime,
    checkpoints: tuple[str, ...] = ("D5",),
) -> None:
    """A finished outcome with one grade per checkpoint, no proposal needed for the read path."""
    from stockbrain.db.models.proposals import TradeProposal
    from stockbrain.enums import OrderSide, OrderType, PriceSource, ProposalStatus

    thesis_id = await _publish(
        db, value, action=action, completed_at=graded_at - dt.timedelta(days=9)
    )
    async with db.session() as session:
        run_id = await session.scalar(
            sa.select(Thesis.research_run_id).where(Thesis.id == thesis_id)
        )
    assert run_id is not None
    entry = graded_at - dt.timedelta(days=8)
    proposal = TradeProposal(
        thesis_id=thesis_id,
        research_run_id=run_id,
        broker=Broker.TRADING212,
        broker_ticker=value.company.broker_ticker,
        account_id="4242",
        broker_environment="demo",
        side=OrderSide.BUY if action is ThesisAction.BUY else OrderSide.SELL,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("1"),
        reference_price=Decimal("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=entry,
        quote_age_ms=0,
        estimated_notional=Decimal("100"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=entry,
        expires_at=entry + dt.timedelta(days=1),
        research_action=action.value,
        research_confidence=0.72,
    )
    outcome_id = uuid.uuid4()
    async with db.transaction() as session:
        session.add(proposal)
        await session.flush()
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=thesis_id,
                research_run_id=run_id,
                company_id=value.company.company_id,
                broker_instrument_id=value.company.broker_instrument_id,
                broker=Broker.TRADING212,
                broker_ticker=value.company.broker_ticker,
                event_type=event_type,
                action=action,
                horizon=TimeHorizon.WEEKS,
                confidence=Decimal("0.72"),
                entry_at=entry,
                entry_date=entry.date(),
                currency="USD",
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        for index, checkpoint in enumerate(checkpoints):
            session.add(
                ThesisOutcomeGrade(
                    outcome_id=outcome_id,
                    checkpoint=checkpoint,
                    trading_days=5 * (index + 1),
                    entry_close=Decimal("100"),
                    current_close=Decimal("100"),
                    benchmark_entry_close=Decimal("100"),
                    benchmark_current_close=Decimal("100"),
                    instrument_return=Decimal(0),
                    benchmark_return=Decimal(0),
                    alpha=Decimal(alpha),
                    correct=correct,
                    graded_at=graded_at + dt.timedelta(minutes=index),
                )
            )


async def test_calibration_rows_report_the_company_and_the_event_type(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == value.event_id).values(event_type="REGULATORY")
        )
    before = value.as_of - dt.timedelta(days=1)
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="REGULATORY",
        correct=True,
        alpha="0.02",
        graded_at=before,
    )
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.REDUCE,
        event_type="REGULATORY",
        correct=False,
        alpha="0.01",
        graded_at=before,
    )
    # Graded after as_of: invisible to this run.
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="REGULATORY",
        correct=False,
        alpha="-0.5",
        graded_at=value.as_of + dt.timedelta(days=1),
    )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    company = built.memory.company_record
    assert company is not None
    assert (company.samples, company.correct) == (1, 1)
    by_key = {row.key: row for row in built.memory.event_type_record}
    assert by_key["REGULATORY×BUY"].samples == 1  # noqa: RUF001
    assert by_key["REGULATORY×REDUCE"].hit_rate == Decimal("0")  # noqa: RUF001
    assert "REGULATORY×SELL" not in by_key  # noqa: RUF001


async def test_the_close_grade_stands_for_an_outcome_over_its_checkpoints(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    before = value.as_of - dt.timedelta(days=1)
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="EARNINGS",
        correct=True,
        alpha="0.03",
        graded_at=before,
        checkpoints=("D5", "CLOSE"),
    )
    # Both grades were written with the same values; make CLOSE the incorrect one.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ThesisOutcomeGrade)
            .where(ThesisOutcomeGrade.checkpoint == "CLOSE")
            .values(correct=False, alpha=Decimal("-0.02"))
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    company = built.memory.company_record
    assert company is not None
    assert (company.samples, company.correct, company.mean_alpha) == (
        1,
        0,
        Decimal("-0.02"),
    )
