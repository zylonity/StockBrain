"""Exit sweep: peak tracking, origin-thesis recovery and proposal generation."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.broker.account_state import AccountStateService
from stockbrain.broker.trading212_account import (
    T212AccountSummary,
    T212Position,
    Trading212AccountClient,
)
from stockbrain.db.models.portfolio import PositionPeak
from stockbrain.db.session import Database
from stockbrain.enums import Broker

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
