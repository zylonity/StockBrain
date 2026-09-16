"""The memory sweep: record executed thesis trades, grade them from bars."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    OutcomeStatus,
    PriceSource,
    ProposalStatus,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.errors import ProviderResponseError
from stockbrain.intelligence.memory import MemoryService
from stockbrain.market_data.base import Bar
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration

ENTRY = dt.datetime(2026, 9, 1, 15, tzinfo=dt.UTC)


def _bar(symbol: str, day: dt.date, close: str) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
    )


def _series(symbol: str, start: dt.date, closes: Sequence[str]) -> list[Bar]:
    """One bar per weekday from ``start``."""
    bars: list[Bar] = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        bars.append(_bar(symbol, day, close))
        day += dt.timedelta(days=1)
    return bars


class FakeBars:
    def __init__(self, series: dict[str, tuple[list[Bar], str]]) -> None:
        self.series = series
        self.calls: list[str] = []
        self.days: list[int] = []

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        self.calls.append(symbol)
        self.days.append(days)
        if symbol not in self.series:
            raise ProviderResponseError(f"no bars for {symbol}")
        return self.series[symbol]


def _settings(**overrides: object) -> Settings:
    return ph.settings(memory_grade_enabled=True, **overrides)


async def _executed(
    database: Database,
    *,
    action: ThesisAction = ThesisAction.BUY,
    horizon: TimeHorizon = TimeHorizon.WEEKS,
    executed_at: dt.datetime = ENTRY,
    risk_rules: list[dict[str, object]] | None = None,
) -> uuid.UUID:
    await ph.seed(database, action=action)
    async with database.transaction() as session:
        thesis = await session.get(ph.Thesis, ph.THESIS_ID)  # type: ignore[attr-defined]
        assert thesis is not None
        thesis.time_horizon = horizon
    proposal = TradeProposal(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        broker=Broker.TRADING212,
        broker_ticker="AAPL_US_EQ",
        account_id=ph.ACCOUNT_ID,
        broker_environment="demo",
        side=OrderSide.BUY if action is ThesisAction.BUY else OrderSide.SELL,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("2"),
        reference_price=Decimal("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=executed_at,
        quote_age_ms=0,
        estimated_notional=Decimal("200"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=executed_at,
        expires_at=executed_at + dt.timedelta(days=1),
        research_action=action.value,
        research_confidence=0.7,
        risk_rules=risk_rules or [],
    )
    async with database.transaction() as session:
        session.add(proposal)
    return proposal.id


async def _hold(database: Database, quantity: Decimal = Decimal("2")) -> None:
    await ph.fund(database, positions={"AAPL_US_EQ": (quantity, quantity)})


async def _outcomes(database: Database) -> list[ThesisOutcome]:
    async with database.session() as session:
        return list((await session.scalars(sa.select(ThesisOutcome))).all())


async def _grades(database: Database) -> list[ThesisOutcomeGrade]:
    async with database.session() as session:
        return list(
            (
                await session.scalars(
                    sa.select(ThesisOutcomeGrade).order_by(ThesisOutcomeGrade.trading_days)
                )
            ).all()
        )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
async def test_an_executed_thesis_trade_is_recorded_once(clean_tables: Database) -> None:
    proposal_id = await _executed(clean_tables)
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    assert await service.record() == 1
    assert await service.record() == 0
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.proposal_id == proposal_id
    assert outcome.action is ThesisAction.BUY
    assert outcome.horizon is TimeHorizon.WEEKS
    assert outcome.entry_date == ENTRY.date()
    assert outcome.benchmark_symbol == "SPY"
    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.is_exit is False


async def test_an_exit_sweep_proposal_is_recorded_as_an_exit(clean_tables: Database) -> None:
    await _executed(
        clean_tables,
        action=ThesisAction.SELL,
        risk_rules=[{"rule_id": "hard_stop", "outcome": "BLOCK"}],
    )
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    await service.record()
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.is_exit is True
    assert outcome.exit_rule_id == "hard_stop"


async def test_a_proposal_without_a_thesis_is_not_recorded(clean_tables: Database) -> None:
    await _executed(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(TradeProposal).values(thesis_id=None))
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    assert await service.record() == 0


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _world(*, instrument: Sequence[str], benchmark: Sequence[str]) -> FakeBars:
    start = dt.date(2026, 8, 31)
    return FakeBars(
        {
            "AAPL": (_series("AAPL", start, instrument), "USD"),
            "SPY": (_series("SPY", start, benchmark), "USD"),
        }
    )


async def test_a_weeks_buy_is_graded_at_d5_once_five_sessions_have_passed(
    clean_tables: Database,
) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    # Entry Tue 1 Sep at 100; D5 is Tue 8 Sep.  Instrument +10%, SPY +2%.
    bars = _world(
        instrument=["99", "100", "101", "102", "103", "104", "110", "111"],
        benchmark=["500", "500", "501", "502", "503", "504", "510", "511"],
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC))
    assert tally["graded"] == 1
    (grade,) = await _grades(clean_tables)
    assert grade.checkpoint == "D5"
    assert grade.entry_close == Decimal("100")
    assert grade.current_close == Decimal("111")
    assert grade.instrument_return == Decimal("0.11")
    assert grade.benchmark_return == Decimal("0.022")
    assert grade.alpha == Decimal("0.088")
    assert grade.correct is True
    assert sorted(bars.calls) == ["AAPL", "SPY"]


async def test_too_few_sessions_means_no_grade_yet(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(instrument=["99", "100", "101"], benchmark=["500", "500", "501"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 3, 22, tzinfo=dt.UTC))
    assert tally["graded"] == 0
    assert await _grades(clean_tables) == []


async def test_a_checkpoint_is_graded_once(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(
        instrument=["99", "100", "101", "102", "103", "104", "110", "111"],
        benchmark=["500", "500", "501", "502", "503", "504", "510", "511"],
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    first = dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC)
    await service.grade(now=first)
    # A second tick a day later re-fetches (the 20h clock has elapsed) but
    # finds D5 already graded and D20 not yet due.
    tally = await service.grade(now=first + dt.timedelta(days=1))
    assert tally["graded"] == 0
    assert len(await _grades(clean_tables)) == 1


async def test_a_reduce_is_correct_when_the_instrument_then_lags(clean_tables: Database) -> None:
    await _executed(clean_tables, action=ThesisAction.REDUCE, horizon=TimeHorizon.DAYS)
    await _hold(clean_tables)
    # D1 for a DAYS horizon.  Instrument -5%, SPY +1% -> alpha -6% -> correct trim.
    bars = _world(instrument=["99", "100", "95"], benchmark=["500", "500", "505"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 2, 22, tzinfo=dt.UTC))
    (grade,) = await _grades(clean_tables)
    assert (grade.checkpoint, grade.correct) == ("D1", True)


async def test_a_buy_whose_position_is_gone_is_closed_and_graded(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await ph.fund(clean_tables)  # no positions: the holding is gone
    bars = _world(instrument=["99", "100", "90"], benchmark=["500", "500", "500"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 2, 22, tzinfo=dt.UTC))
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.close_reason == "position_gone"
    grades = await _grades(clean_tables)
    assert [grade.checkpoint for grade in grades] == ["CLOSE"]
    assert grades[0].correct is False


async def test_a_close_takes_its_reason_and_date_from_the_exit_sell(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await ph.fund(clean_tables)
    sold_at = dt.datetime(2026, 9, 3, 15, tzinfo=dt.UTC)
    async with clean_tables.transaction() as session:
        session.add(
            TradeProposal(
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                account_id=ph.ACCOUNT_ID,
                broker_environment="demo",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                proposed_quantity=Decimal("2"),
                reference_price=Decimal("92"),
                reference_currency="USD",
                price_source=PriceSource.ALPACA_IEX,
                quote_timestamp=sold_at,
                quote_age_ms=0,
                estimated_notional=Decimal("184"),
                account_currency="USD",
                status=ProposalStatus.EXECUTED,
                executed_at=sold_at,
                expires_at=sold_at + dt.timedelta(days=1),
                research_action="SELL",
                research_confidence=0.7,
                risk_rules=[{"rule_id": "trailing_stop", "outcome": "BLOCK"}],
            )
        )
    bars = _world(instrument=["99", "100", "95", "92", "120"], benchmark=["500"] * 5)
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 5, 22, tzinfo=dt.UTC))
    async with clean_tables.session() as session:
        outcome = (
            await session.scalars(
                sa.select(ThesisOutcome).where(ThesisOutcome.action == ThesisAction.BUY)
            )
        ).one()
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.close_reason == "trailing_stop"
    assert outcome.closed_at == sold_at
    grades = [g for g in await _grades(clean_tables) if g.outcome_id == outcome.id]
    assert [g.checkpoint for g in grades] == ["CLOSE"]
    assert grades[0].current_close == Decimal("92")  # the close on the sell date, not the rebound


async def test_an_unknown_symbol_abandons_the_outcome(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ph.BrokerInstrument).values(exchange="Nowhere Exchange")  # type: ignore[attr-defined]
        )
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    await service.record()
    tally = await service.grade()
    assert tally["abandoned"] == 1
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.ABANDONED
    assert outcome.close_reason == "no_yahoo_symbol"


async def test_a_provider_failure_degrades_one_symbol_and_the_tick_survives(
    clean_tables: Database,
) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = FakeBars({"SPY": (_series("SPY", dt.date(2026, 8, 31), ["500"] * 8), "USD")})
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC))
    assert tally["failed"] == 1
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.last_attempt_at is not None


async def test_a_symbol_is_not_refetched_within_twenty_hours(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(instrument=["99", "100", "101"], benchmark=["500", "500", "501"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    first = dt.datetime(2026, 9, 3, 22, tzinfo=dt.UTC)
    await service.grade(now=first)
    calls = len(bars.calls)
    await service.grade(now=first + dt.timedelta(hours=2))
    assert len(bars.calls) == calls


async def test_an_entry_older_than_the_fetch_window_is_not_graded_against_the_wrong_bar(
    clean_tables: Database,
) -> None:
    """A series that starts after the entry date must not donate its oldest bar."""
    await _executed(clean_tables)
    await _hold(clean_tables)
    # 2026-09-09 is the first session more than ENTRY_BAR_TOLERANCE_DAYS (7)
    # calendar days after the 2026-09-01 entry, so the entry bar is rejected.
    start = dt.date(2026, 9, 9)
    bars = FakeBars(
        {
            "AAPL": (_series("AAPL", start, ["100"] * 8), "USD"),
            "SPY": (_series("SPY", start, ["500"] * 8), "USD"),
        }
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 18, 22, tzinfo=dt.UTC))
    assert tally["failed"] == 1
    assert await _grades(clean_tables) == []
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.last_attempt_at is not None


async def test_the_fetch_window_grows_to_cover_the_oldest_pending_entry(
    clean_tables: Database,
) -> None:
    old = ENTRY - dt.timedelta(days=200)
    await _executed(clean_tables, executed_at=old)
    bars = FakeBars(
        {
            "AAPL": (_series("AAPL", old.date(), ["100"]), "USD"),
            "SPY": (_series("SPY", old.date(), ["500"]), "USD"),
        }
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=ENTRY)
    assert bars.days == [210, 210]
    assert all(days == 210 for days in bars.days)


async def test_an_entry_older_than_the_maximum_window_is_abandoned(
    clean_tables: Database,
) -> None:
    await _executed(clean_tables, executed_at=ENTRY - dt.timedelta(days=401))
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    await service.record()
    tally = await service.grade(now=ENTRY)
    assert tally["abandoned"] == 1
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.close_reason == "entry_outside_window"


async def test_a_trim_retires_after_its_last_checkpoint(clean_tables: Database) -> None:
    await _executed(clean_tables, action=ThesisAction.REDUCE, horizon=TimeHorizon.DAYS)
    await _hold(clean_tables)
    # DAYS checkpoints are D1 and D5; eight sessions from 31 Aug clear both.
    bars = _world(
        instrument=["99", "100", "101", "102", "103", "104", "110", "111"],
        benchmark=["500", "500", "501", "502", "503", "504", "510", "511"],
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    moment = dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC)
    await service.grade(now=moment)
    grades = await _grades(clean_tables)
    assert {grade.checkpoint for grade in grades} == {"D1", "D5"}
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.close_reason == "checkpoints_complete"
    assert outcome.closed_at == moment
