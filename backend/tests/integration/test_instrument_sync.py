"""Trading 212 metadata sync against a real database.

The properties under test are the ones a job queue with at-least-once delivery
demands: a repeated sync must change nothing, a concurrent sync must not
duplicate or briefly empty the table instrument resolution reads, and a
disappearing instrument must be retired rather than deleted.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.broker.instrument_sync import InstrumentSyncService
from stockbrain.broker.trading212_metadata import T212Exchange, T212Instrument
from stockbrain.db.models.companies import BrokerExchange, BrokerInstrument, BrokerWorkingSchedule
from stockbrain.db.session import Database
from stockbrain.enums import Broker

pytestmark = pytest.mark.integration

EXCHANGES = [
    {
        "id": 331,
        "name": "NASDAQ",
        "workingSchedules": [
            {
                "id": 74,
                "timeEvents": [
                    {"date": "2026-09-04T08:00:00Z", "type": "PRE_MARKET_OPEN"},
                    {"date": "2026-09-04T13:30:00Z", "type": "OPEN"},
                    {"date": "2026-09-04T20:00:00Z", "type": "CLOSE"},
                ],
            }
        ],
    },
    {
        "id": 332,
        "name": "London Stock Exchange",
        "workingSchedules": [{"id": 91, "timeEvents": []}],
    },
]

INSTRUMENTS = [
    {
        "addedOn": "2019-08-24T14:15:22Z",
        "currencyCode": "USD",
        "extendedHours": True,
        "isin": "US0378331005",
        "maxOpenQuantity": 55000.0,
        "name": "Apple Inc.",
        "shortName": "AAPL",
        "ticker": "AAPL_US_EQ",
        "type": "STOCK",
        "workingScheduleId": 74,
    },
    {
        "currencyCode": "GBX",
        "extendedHours": False,
        "isin": "GB0007980591",
        "maxOpenQuantity": 100.5,
        "name": "BP p.l.c.",
        "shortName": "BP.",
        "ticker": "BPl_EQ",
        "type": "STOCK",
        "workingScheduleId": 91,
    },
    {
        # No working schedule: the exchange must stay NULL, never be guessed.
        "currencyCode": "USD",
        "extendedHours": False,
        "isin": "US9999999999",
        "name": "Orphan Corp",
        "shortName": "ORPH",
        "ticker": "ORPH_US_EQ",
        "type": "STOCK",
    },
]


class StubMetadataClient:
    """Answers with whatever the test hands it, and counts calls."""

    def __init__(
        self,
        instruments: list[dict[str, object]] | None = None,
        exchanges: list[dict[str, object]] | None = None,
    ) -> None:
        self.instruments = instruments if instruments is not None else INSTRUMENTS
        self.exchanges = exchanges if exchanges is not None else EXCHANGES
        self.calls = 0

    async def fetch_instruments(self) -> list[T212Instrument]:
        self.calls += 1
        return [T212Instrument.model_validate(item) for item in self.instruments]

    async def fetch_exchanges(self) -> list[T212Exchange]:
        return [T212Exchange.model_validate(item) for item in self.exchanges]

    def rate_limit_snapshot(self) -> dict[str, object]:
        return {"limit": 1, "period": "50", "remaining": 0}


def _service(database: Database, client: StubMetadataClient) -> InstrumentSyncService:
    return InstrumentSyncService(database, client)  # type: ignore[arg-type]


async def test_a_sync_populates_instruments_exchanges_and_schedules(
    clean_tables: Database,
) -> None:
    client = StubMetadataClient()
    result = await _service(clean_tables, client).sync()

    assert result.instruments_written == 3
    assert result.exchanges == 2
    assert result.schedules == 2
    assert result.rate_limit == {"limit": 1, "period": "50", "remaining": 0}

    async with clean_tables.session() as session:
        rows = {
            row.broker_ticker: row
            for row in (await session.execute(sa.select(BrokerInstrument))).scalars()
        }
        exchanges = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerExchange))
        ).scalar_one()
        schedules = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerWorkingSchedule))
        ).scalar_one()

    assert exchanges == 2
    assert schedules == 2

    apple = rows["AAPL_US_EQ"]
    assert apple.name == "Apple Inc."
    assert apple.isin == "US0378331005"
    assert apple.currency == "USD"
    assert apple.instrument_type == "STOCK"
    assert apple.extended_hours is True
    assert apple.working_schedule_id == 74
    assert apple.max_open_quantity == Decimal("55000")
    assert apple.is_active is True
    assert apple.last_refreshed_at is not None
    # Derived, not supplied.
    assert apple.exchange == "NASDAQ"
    assert apple.exchange_id == 331
    assert apple.market_symbol == "AAPL"
    assert apple.market_code == "US"
    # The raw payload is preserved for audit.
    assert apple.raw_metadata["ticker"] == "AAPL_US_EQ"


async def test_the_absent_min_trade_quantity_stays_null(clean_tables: Database) -> None:
    """The current API does not report it. Metadata is never invented."""
    await _service(clean_tables, StubMetadataClient()).sync()
    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "AAPL_US_EQ")
            )
        ).scalar_one()
    assert row.min_trade_quantity is None


async def test_a_quantity_is_decimal_not_a_binary_float(clean_tables: Database) -> None:
    await _service(clean_tables, StubMetadataClient()).sync()
    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "BPl_EQ")
            )
        ).scalar_one()
    assert row.max_open_quantity == Decimal("100.5")
    assert isinstance(row.max_open_quantity, Decimal)


async def test_an_instrument_without_a_schedule_has_no_exchange(
    clean_tables: Database,
) -> None:
    """Trading 212 puts no exchange on an instrument; a missing schedule means
    the exchange is unknown, not inferrable from the ticker."""
    result = await _service(clean_tables, StubMetadataClient()).sync()
    assert result.without_exchange == 1

    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "ORPH_US_EQ")
            )
        ).scalar_one()
    assert row.exchange is None
    assert row.exchange_id is None
    # The derived venue code from the ticker is still recorded, separately.
    assert row.market_code == "US"


async def test_repeating_a_sync_creates_no_duplicates(clean_tables: Database) -> None:
    client = StubMetadataClient()
    service = _service(clean_tables, client)
    await service.sync()
    await service.sync()
    await service.sync()

    async with clean_tables.session() as session:
        instruments = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerInstrument))
        ).scalar_one()
        exchanges = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerExchange))
        ).scalar_one()
        schedules = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerWorkingSchedule))
        ).scalar_one()
        active = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(BrokerInstrument)
                .where(BrokerInstrument.is_active.is_(True))
            )
        ).scalar_one()

    assert instruments == 3
    assert exchanges == 2
    assert schedules == 2
    assert active == 3, "a repeat sync must not retire the rows it just wrote"


async def test_concurrent_syncs_converge_without_duplicating(
    clean_tables: Database,
) -> None:
    """Two workers claiming the same redelivered refresh job.

    Everything is an upsert with no delete-then-insert, so the table instrument
    resolution reads is never briefly empty and never gains a second row.
    """
    service = _service(clean_tables, StubMetadataClient())
    await asyncio.gather(*(service.sync() for _ in range(4)))

    async with clean_tables.session() as session:
        instruments = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerInstrument))
        ).scalar_one()
        exchanges = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerExchange))
        ).scalar_one()
    assert instruments == 3
    assert exchanges == 2


async def test_a_disappearing_instrument_is_retired_not_deleted(
    clean_tables: Database,
) -> None:
    """A proposal or an execution attempt may reference the row forever."""
    await _service(clean_tables, StubMetadataClient()).sync()
    remaining = [item for item in INSTRUMENTS if item["ticker"] != "BPl_EQ"]
    result = await _service(clean_tables, StubMetadataClient(instruments=remaining)).sync()

    assert result.deactivated == 1
    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "BPl_EQ")
            )
        ).scalar_one()
        total = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerInstrument))
        ).scalar_one()
    assert row.is_active is False
    assert total == 3, "the row still exists; only its availability changed"


async def test_a_returning_instrument_is_reactivated(clean_tables: Database) -> None:
    remaining = [item for item in INSTRUMENTS if item["ticker"] != "BPl_EQ"]
    await _service(clean_tables, StubMetadataClient(instruments=remaining)).sync()
    await _service(clean_tables, StubMetadataClient()).sync()

    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "BPl_EQ")
            )
        ).scalar_one()
    assert row.is_active is True


async def test_an_empty_response_never_retires_the_whole_universe(
    clean_tables: Database,
) -> None:
    """A provider answering with nothing is a provider fault.

    Treating it as "every instrument was delisted" would take instrument
    resolution down for the entire system on one bad response.
    """
    await _service(clean_tables, StubMetadataClient()).sync()
    result = await _service(clean_tables, StubMetadataClient(instruments=[])).sync()

    assert result.instruments_written == 0
    assert result.deactivated == 0
    async with clean_tables.session() as session:
        active = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(BrokerInstrument)
                .where(BrokerInstrument.is_active.is_(True))
            )
        ).scalar_one()
    assert active == 3


async def test_a_renamed_exchange_updates_in_place(clean_tables: Database) -> None:
    await _service(clean_tables, StubMetadataClient()).sync()
    renamed = [{**EXCHANGES[0], "name": "Nasdaq Global Select"}, EXCHANGES[1]]
    await _service(clean_tables, StubMetadataClient(exchanges=renamed)).sync()

    async with clean_tables.session() as session:
        exchanges = (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerExchange))
        ).scalar_one()
        row = (
            await session.execute(
                sa.select(BrokerInstrument).where(BrokerInstrument.broker_ticker == "AAPL_US_EQ")
            )
        ).scalar_one()
    assert exchanges == 2
    assert row.exchange == "Nasdaq Global Select"


async def test_the_broker_ticker_uniqueness_is_enforced_by_the_database(
    clean_tables: Database,
) -> None:
    """Application guards are necessary but not sufficient; the index is the guarantee."""
    from sqlalchemy.exc import IntegrityError

    await _service(clean_tables, StubMetadataClient()).sync()
    with pytest.raises(IntegrityError):
        async with clean_tables.transaction() as session:
            session.add(
                BrokerInstrument(
                    broker=Broker.TRADING212,
                    broker_ticker="AAPL_US_EQ",
                    name="Impostor",
                )
            )
