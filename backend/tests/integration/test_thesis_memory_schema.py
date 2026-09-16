"""The memory tables exist, round-trip an outcome with a grade, and truncate."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    OutcomeCheckpoint,
    OutcomeStatus,
    PriceSource,
    ProposalStatus,
    ThesisAction,
    TimeHorizon,
)
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


async def _executed_buy(database: Database) -> TradeProposal:
    await ph.seed(database)
    moment = utcnow()
    proposal = TradeProposal(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        broker=Broker.TRADING212,
        broker_ticker="AAPL_US_EQ",
        account_id=ph.ACCOUNT_ID,
        broker_environment="demo",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("2"),
        reference_price=Decimal("180.50"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=moment,
        quote_age_ms=0,
        estimated_notional=Decimal("361"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=moment,
        expires_at=moment + dt.timedelta(days=1),
    )
    async with database.transaction() as session:
        session.add(proposal)
    return proposal


async def test_an_outcome_and_its_grade_round_trip(clean_tables: Database) -> None:
    proposal = await _executed_buy(clean_tables)
    moment = utcnow()
    outcome_id = uuid.uuid4()
    async with clean_tables.transaction() as session:
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                company_id=ph.COMPANY_ID,
                broker_instrument_id=ph.INSTRUMENT_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                event_type="EARNINGS",
                action=ThesisAction.BUY,
                horizon=TimeHorizon.WEEKS,
                confidence=Decimal("0.700"),
                entry_at=moment,
                entry_date=moment.date(),
                reference_price=Decimal("180.50"),
                currency="USD",
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        session.add(
            ThesisOutcomeGrade(
                outcome_id=outcome_id,
                checkpoint=OutcomeCheckpoint.D5.value,
                trading_days=5,
                entry_close=Decimal("180"),
                current_close=Decimal("189"),
                benchmark_entry_close=Decimal("500"),
                benchmark_current_close=Decimal("505"),
                instrument_return=Decimal("0.05"),
                benchmark_return=Decimal("0.01"),
                alpha=Decimal("0.04"),
                correct=True,
                graded_at=moment,
            )
        )
    async with clean_tables.session() as session:
        row = await session.get(ThesisOutcome, outcome_id)
        assert row is not None
        assert row.status is OutcomeStatus.PENDING
        grades = (
            await session.scalars(
                sa.select(ThesisOutcomeGrade).where(ThesisOutcomeGrade.outcome_id == outcome_id)
            )
        ).all()
        assert [grade.checkpoint for grade in grades] == ["D5"]
        assert grades[0].alpha == Decimal("0.040000")


async def test_a_second_grade_for_the_same_checkpoint_is_refused(clean_tables: Database) -> None:
    proposal = await _executed_buy(clean_tables)
    moment = utcnow()
    outcome_id = uuid.uuid4()

    def grade() -> ThesisOutcomeGrade:
        return ThesisOutcomeGrade(
            outcome_id=outcome_id,
            checkpoint="D1",
            trading_days=1,
            entry_close=Decimal(1),
            current_close=Decimal(1),
            benchmark_entry_close=Decimal(1),
            benchmark_current_close=Decimal(1),
            instrument_return=Decimal(0),
            benchmark_return=Decimal(0),
            alpha=Decimal(0),
            correct=False,
            graded_at=moment,
        )

    async with clean_tables.transaction() as session:
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                company_id=ph.COMPANY_ID,
                broker_instrument_id=ph.INSTRUMENT_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                action=ThesisAction.BUY,
                horizon=TimeHorizon.DAYS,
                confidence=Decimal("0.7"),
                entry_at=moment,
                entry_date=moment.date(),
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        session.add(grade())
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_tables.transaction() as session:
            session.add(grade())
