"""Generation feeds the engine the bucket for the thesis's (event_type, action)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.intelligence.memory import MemoryService
from tests import proposal_helpers as ph
from tests.integration.test_thesis_memory_sweep import FakeBars, _settings

pytestmark = pytest.mark.integration


async def _poor_record(db: Database, *, event_type: str, samples: int) -> None:
    """``samples`` graded BUY outcomes for ``event_type``, all wrong, on other listings."""
    from stockbrain.db.models.companies import BrokerInstrument, Company
    from stockbrain.db.models.research import ResearchRun, Thesis
    from stockbrain.enums import ResearchStatus

    moment = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    async with db.transaction() as session:
        for index in range(samples):
            company_id, instrument_id = uuid.uuid4(), uuid.uuid4()
            run_id, thesis_id, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            ticker = f"X{index}_US_EQ"
            session.add(Company(id=company_id, name=f"X{index}", isin=f"US000000000{index}"))
            session.add(
                Event(
                    id=event_id,
                    title=f"e{index}",
                    title_hash=f"{index:064d}",
                    summary="",
                    first_seen_at=moment,
                    event_type=event_type,
                )
            )
            await session.flush()
            session.add(
                BrokerInstrument(
                    id=instrument_id,
                    company_id=company_id,
                    broker=Broker.TRADING212,
                    broker_ticker=ticker,
                    market_symbol=f"X{index}",
                    name=f"X{index}",
                    exchange="NASDAQ",
                    currency="USD",
                    isin=f"US000000000{index}",
                    instrument_type="STOCK",
                    is_active=True,
                )
            )
            session.add(
                ResearchRun(
                    id=run_id,
                    event_id=event_id,
                    company_id=company_id,
                    broker_instrument_id=instrument_id,
                    status=ResearchStatus.SUCCEEDED,
                    completed_at=moment,
                )
            )
            await session.flush()
            session.add(
                Thesis(
                    id=thesis_id,
                    research_run_id=run_id,
                    action=ThesisAction.BUY,
                    confidence=0.8,
                    time_horizon=TimeHorizon.WEEKS,
                    summary="s",
                )
            )
            proposal = TradeProposal(
                thesis_id=thesis_id,
                research_run_id=run_id,
                broker=Broker.TRADING212,
                broker_ticker=ticker,
                account_id=ph.ACCOUNT_ID,
                broker_environment="demo",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                proposed_quantity=Decimal(1),
                reference_price=Decimal(10),
                reference_currency="USD",
                price_source=PriceSource.ALPACA_IEX,
                quote_timestamp=moment,
                quote_age_ms=0,
                estimated_notional=Decimal(10),
                account_currency="USD",
                status=ProposalStatus.EXECUTED,
                executed_at=moment,
                expires_at=moment + dt.timedelta(days=1),
                research_action="BUY",
                research_confidence=0.8,
            )
            session.add(proposal)
            await session.flush()
            outcome_id = uuid.uuid4()
            session.add(
                ThesisOutcome(
                    id=outcome_id,
                    proposal_id=proposal.id,
                    thesis_id=thesis_id,
                    research_run_id=run_id,
                    company_id=company_id,
                    broker_instrument_id=instrument_id,
                    broker=Broker.TRADING212,
                    broker_ticker=ticker,
                    event_type=event_type,
                    action=ThesisAction.BUY,
                    horizon=TimeHorizon.WEEKS,
                    confidence=Decimal("0.8"),
                    entry_at=moment,
                    entry_date=moment.date(),
                    currency="USD",
                    benchmark_symbol="SPY",
                )
            )
            await session.flush()
            session.add(
                ThesisOutcomeGrade(
                    outcome_id=outcome_id,
                    checkpoint="D5",
                    trading_days=5,
                    entry_close=Decimal(10),
                    current_close=Decimal(9),
                    benchmark_entry_close=Decimal(10),
                    benchmark_current_close=Decimal(10),
                    instrument_return=Decimal("-0.1"),
                    benchmark_return=Decimal(0),
                    alpha=Decimal("-0.1"),
                    correct=False,
                    graded_at=moment + dt.timedelta(days=7),
                )
            )


def _service(db: Database) -> ph.ProposalService:  # type: ignore[name-defined]
    memory = MemoryService(db, _settings(), bars=FakeBars({}))
    return ph.service(db, memory=memory)


async def test_generation_records_the_calibration_rule_on_the_proposal(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == ph.EVENT_ID).values(event_type="REGULATORY")
        )
    await _poor_record(clean_tables, event_type="REGULATORY", samples=10)
    service = _service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    rules = {rule["rule_id"]: rule for rule in proposal.risk_rules}
    assert rules["calibration_size_modulation"]["outcome"] == "REDUCE"
    assert rules["calibration_size_modulation"]["size_factor"] == "0.5"


async def test_a_thin_record_leaves_the_size_alone(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == ph.EVENT_ID).values(event_type="REGULATORY")
        )
    await _poor_record(clean_tables, event_type="REGULATORY", samples=4)
    service = _service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    rules = {rule["rule_id"]: rule for rule in proposal.risk_rules}
    assert rules["calibration_size_modulation"]["outcome"] == "WARN"


async def test_no_memory_service_means_no_rule(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    assert "calibration_size_modulation" not in {rule["rule_id"] for rule in proposal.risk_rules}
