"""Exit sweep: peak tracking, origin-thesis recovery and proposal generation."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.broker.account_state import AccountStateService
from stockbrain.broker.trading212_account import (
    T212AccountSummary,
    T212Position,
    Trading212AccountClient,
)
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    RuleOutcome,
    ThesisAction,
)
from stockbrain.proposals.service import ProposalService
from stockbrain.risk.exits import ExitSignal
from stockbrain.risk.models import RuleResult
from tests import proposal_helpers as ph

# The documented payload shape, mirroring the double in
# ``tests/unit/test_trading212_account.py``.  Only the fields ``sync`` reads are
# present, and they are nested under ``instrument``/``walletImpact`` because
# that is the shape Trading 212 currently returns.
_SUMMARY = {
    "id": 4242,
    "currency": "GBP",
    "totalValue": 9234.81,
    "cash": {"availableToTrade": 1234.56, "reservedForOrders": 0.0, "inPies": 0.0},
    "investments": {"currentValue": 8000.25, "unrealizedProfitLoss": 500.25},
}

_POSITION = {
    "instrument": {"ticker": "AAPL_US_EQ", "currency": "USD"},
    "quantity": 12.5,
    "quantityAvailableForTrading": 12.5,
    "quantityInPies": 0.0,
    "averagePricePaid": 180.5,
    "currentPrice": 200.25,
    "createdAt": dt.datetime(2026, 1, 5, 9, 30, tzinfo=dt.UTC),
    "walletImpact": {"currency": "GBP", "currentValue": 1975.0, "unrealizedProfitLoss": 175.0},
}


class _FakeAccountClient(Trading212AccountClient):
    """The real client with both reads answered from memory.

    Modelled on the double in ``tests/unit/test_trading212_account.py``: the
    documented payloads are parsed through the real pydantic models, so ``sync``
    sees exactly the fields the broker would hand it.  ``prices`` and
    ``positions`` are mutable so a test can move the market between syncs.
    """

    def __init__(self, *, prices: list[Decimal]) -> None:
        self.prices = prices
        self.positions: list[dict[str, object]] = [dict(_POSITION)]

    async def fetch_account_summary(self) -> T212AccountSummary:
        return T212AccountSummary.model_validate(_SUMMARY)

    async def fetch_positions(self) -> list[T212Position]:
        return [
            T212Position.model_validate({**payload, "currentPrice": self.prices[index]})
            for index, payload in enumerate(self.positions)
        ]


class _AccountService(AccountStateService):
    """The real service wired to :class:`_FakeAccountClient`.

    ``client`` is public here so a test can change the fake between syncs.
    """

    def __init__(self, database: Database, *, prices: list[Decimal]) -> None:
        self.client = _FakeAccountClient(prices=prices)
        super().__init__(
            database,
            self.client,
            broker=Broker.TRADING212,
            broker_environment="demo",
        )


async def _account_service(database: Database, *, prices: list[Decimal]) -> _AccountService:
    return _AccountService(database, prices=prices)


async def test_the_peak_ratchets_up_and_never_down(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.prices = [Decimal("130")]
    await service.sync()

    service.client.prices = [Decimal("110")]
    await service.sync()

    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.peak_price == Decimal("130")
    assert peak.observations == 3


async def test_closing_a_position_deletes_its_peak(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.positions = []
    await service.sync()

    async with database.session() as session:
        remaining = (
            await session.execute(sa.select(sa.func.count()).select_from(PositionPeak))
        ).scalar_one()
    assert remaining == 0


async def _proposal_service(database: Database) -> ProposalService:
    """The real service over the shared stub provider, as the proposal tests build it."""
    return ph.service(database)


def _rule(rule_id: str) -> RuleResult:
    """The ``RuleResult`` a fired exit rule carries, in the shape Task 3 emits."""
    return RuleResult(
        rule_id=rule_id,
        rule_version=1,
        outcome=RuleOutcome.WARN,
        reason="the exit rule fired",
        observed="observed",
        threshold="threshold",
    )


@dataclass(frozen=True, slots=True)
class _ExecutedBuy:
    """The lineage a position StockBrain opened leaves behind."""

    thesis_id: uuid.UUID
    research_run_id: uuid.UUID
    proposal_id: uuid.UUID


async def _seed_executed_buy(
    database: Database,
    *,
    broker_ticker: str = "AAPL_US_EQ",
    quantity: Decimal = Decimal("9"),
) -> _ExecutedBuy:
    """Seed a funded, held position and the executed BUY proposal that opened it.

    The research chain is ``proposal_helpers``' healthy world; only the final
    proposal is written directly, because an exit reuses the opening thesis and
    the ordinary generation path would give that row the same dedupe key an
    exit proposes under.
    """
    await ph.seed(database)
    await ph.fund(database, positions={broker_ticker: (quantity, quantity)})
    moment = utcnow()
    proposal = TradeProposal(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        broker=Broker.TRADING212,
        broker_ticker=broker_ticker,
        account_id=ph.ACCOUNT_ID,
        broker_environment=ph.settings().t212_env.value,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=quantity,
        reference_price=Decimal("180.50"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=moment,
        quote_age_ms=0,
        estimated_notional=quantity * Decimal("180.50"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=moment,
        expires_at=moment + dt.timedelta(days=1),
    )
    async with database.transaction() as session:
        session.add(proposal)
    return _ExecutedBuy(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        proposal_id=proposal.id,
    )


async def test_an_exit_proposal_inherits_the_origin_thesis(clean_tables: Database) -> None:
    """Section 17's lineage requirement, satisfied without a schema change: the
    exit proposal points at the thesis the position was opened on."""
    database = clean_tables
    fixture = await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")

    service = await _proposal_service(database)
    signal = ExitSignal(
        rule_id="hard_stop",
        action=ThesisAction.SELL,
        reason="the position is -9.00% against average cost",
        rule=_rule("hard_stop"),
    )
    result = await service.generate_exit("AAPL_US_EQ", signal)

    assert result.created
    async with database.session() as session:
        proposal = await session.get(TradeProposal, result.proposal_id)
    assert proposal is not None
    assert proposal.thesis_id == fixture.thesis_id
    assert proposal.research_run_id == fixture.research_run_id
    assert proposal.side is OrderSide.SELL
    assert {item["reason"] for item in proposal.sizing_reasons} >= {signal.reason}
    assert any(rule["rule_id"] == "hard_stop" for rule in proposal.risk_rules)


async def test_a_position_with_no_origin_proposal_is_not_exited(clean_tables: Database) -> None:
    database = clean_tables
    service = await _proposal_service(database)
    result = await service.generate_exit(
        "MSFT_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="irrelevant",
            rule=_rule("hard_stop"),
        ),
    )
    assert not result.created
    assert result.reason is not None
    assert "no executed StockBrain buy" in result.reason
