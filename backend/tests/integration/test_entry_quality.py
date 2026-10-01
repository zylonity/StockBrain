"""Entry-quality BUY guards: reaction headlines, same-story rebuys, chasing a day move."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import BarTimeframe, ProposalStatus, ThesisAction, TimeHorizon
from stockbrain.market_data.base import Bar
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration

REACTION = "Why Is Apple Stock Soaring Monday?"


async def _headline(db: Database, title: str) -> None:
    async with db.transaction() as session:
        await session.execute(sa.update(Event).where(Event.id == ph.EVENT_ID).values(title=title))


async def _rule_ids(db: Database, evaluation_id: uuid.UUID | None) -> list[str]:
    assert evaluation_id is not None
    async with db.session() as session:
        evaluation = await session.get(RiskEvaluation, evaluation_id)
    assert evaluation is not None
    return [rule["rule_id"] for rule in evaluation.rules]


async def test_a_reaction_headline_cannot_trigger_a_buy(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    await _headline(clean_tables, REACTION)
    market_data = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=market_data, risk_block_reaction_headlines=True)
    result = await service.generate(ph.THESIS_ID)
    assert not result.created
    assert await _rule_ids(clean_tables, result.evaluation_id) == ["entry_headline_quality"]
    # Refused before any market-data spend.
    assert market_data.quote_calls == 0


async def test_the_headline_guard_is_off_by_default(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    await _headline(clean_tables, REACTION)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert result.created


async def test_a_reaction_headline_never_blocks_a_sell(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.SELL, confidence=0.9)
    await ph.fund(clean_tables)
    await _headline(clean_tables, REACTION)
    service = ph.service(clean_tables, risk_block_reaction_headlines=True)
    result = await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        rules = [
            rule["rule_id"]
            for evaluation in (await session.scalars(sa.select(RiskEvaluation))).all()
            for rule in evaluation.rules
        ]
    assert "entry_headline_quality" not in rules
    assert "not a fresh catalyst" not in (result.reason or "")


async def test_the_same_story_is_not_bought_twice(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, risk_same_story_rebuy_days=7)
    first = await service.generate(ph.THESIS_ID)
    assert first.created
    second_thesis = uuid.uuid4()
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(
                status=ProposalStatus.EXECUTED, executed_at=dt.datetime.now(dt.UTC)
            )
        )
        session.add(
            Thesis(
                id=second_thesis,
                research_run_id=ph.RUN_ID,
                action=ThesisAction.BUY,
                confidence=0.9,
                time_horizon=TimeHorizon.DAYS,
            )
        )
    second = await service.generate(second_thesis)
    assert not second.created
    assert await _rule_ids(clean_tables, second.evaluation_id) == ["entry_same_story"]


class _PreviousClose(ph.StubMarketData):
    def __init__(self, close: str) -> None:
        super().__init__()
        self.close = Decimal(close)

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> Sequence[Bar]:
        yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        return [Bar(symbol, yesterday, self.close, self.close, self.close, self.close, 1000)]


async def test_a_buy_after_a_large_day_move_is_refused(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    # The stub quotes a 200.00 mid; 190 -> +5.3% on the day.
    service = ph.service(
        clean_tables, market_data=_PreviousClose("190"), risk_max_buy_day_move_pct=Decimal("0.02")
    )
    result = await service.generate(ph.THESIS_ID)
    assert not result.created
    assert await _rule_ids(clean_tables, result.evaluation_id) == ["entry_day_move"]


async def test_a_small_day_move_does_not_block(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables, market_data=_PreviousClose("199"), risk_max_buy_day_move_pct=Decimal("0.02")
    )
    assert (await service.generate(ph.THESIS_ID)).created
