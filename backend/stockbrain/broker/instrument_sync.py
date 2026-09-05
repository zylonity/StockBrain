"""Populate ``broker_instruments`` from Trading 212 metadata.

Three properties matter more than speed:

* **Idempotent.** Everything is an upsert keyed on
  ``uq_broker_instruments_broker_broker_ticker``.  Running the sync twice
  changes nothing the second time.
* **Concurrency-safe.** There is no delete-then-insert and no read-then-write:
  two syncs racing converge on the same rows, and neither ever briefly empties
  the table that instrument resolution reads.
* **Non-destructive.** An instrument that disappears from the provider is marked
  ``is_active = false``, never deleted.  A proposal, an execution attempt or an
  audit row may reference it forever.

The exchange of an instrument is *derived*: Trading 212's instrument payload has
no exchange field, only a ``workingScheduleId``, so exchanges are synced first
and the schedule map is what fills ``exchange``.  An instrument whose schedule
is absent keeps a NULL exchange rather than one guessed from its ticker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.broker.trading212_metadata import (
    T212Exchange,
    T212Instrument,
    Trading212MetadataClient,
)
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerExchange, BrokerInstrument, BrokerWorkingSchedule
from stockbrain.db.session import Database
from stockbrain.enums import Broker
from stockbrain.instruments.normalize import (
    instrument_name_key,
    normalize_currency,
    normalize_isin,
    normalize_ticker,
    split_broker_ticker,
)
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["InstrumentSyncResult", "InstrumentSyncService", "instrument_row"]

log = get_logger(__name__)

#: Rows per upsert statement.  Trading 212 returns the whole universe in one
#: response (tens of thousands of rows); one giant INSERT would exceed
#: PostgreSQL's bind-parameter limit.
_BATCH_SIZE = 500


@dataclass(slots=True)
class InstrumentSyncResult:
    broker: Broker
    exchanges: int = 0
    schedules: int = 0
    instruments_seen: int = 0
    instruments_written: int = 0
    deactivated: int = 0
    with_exchange: int = 0
    without_exchange: int = 0
    started_at: dt.datetime = field(default_factory=utcnow)
    finished_at: dt.datetime | None = None
    rate_limit: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker.value,
            "exchanges": self.exchanges,
            "schedules": self.schedules,
            "instruments_seen": self.instruments_seen,
            "instruments_written": self.instruments_written,
            "deactivated": self.deactivated,
            "with_exchange": self.with_exchange,
            "without_exchange": self.without_exchange,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "rate_limit": self.rate_limit,
        }


def _decimal(value: float | None) -> Decimal | None:
    """Convert a JSON number to ``Decimal`` without a binary-float detour.

    ``str(float)`` is the shortest decimal that round-trips to the same double,
    which for a value that arrived as a JSON literal is that literal.  Going via
    ``Decimal(float)`` instead would persist ``0.1`` as
    ``0.1000000000000000055511151231257827``.
    """
    if value is None:
        return None
    return Decimal(str(value))


def instrument_row(
    instrument: T212Instrument,
    *,
    broker: Broker,
    exchange_by_schedule: dict[int, tuple[int, str | None]],
    now: dt.datetime,
) -> dict[str, Any]:
    """Map one provider instrument onto a ``broker_instruments`` row.

    Only fields Trading 212 actually supplies are persisted.  ``exchange`` and
    ``market_symbol`` are derived and documented as such on the model;
    ``min_trade_quantity`` stays NULL because the current API does not report it.
    """
    symbol, market_code, _kind = split_broker_ticker(instrument.ticker)
    schedule = exchange_by_schedule.get(instrument.working_schedule_id or -1)
    exchange_id, exchange_name = schedule if schedule else (None, None)

    # `shortName` is the provider's own short label and is the better market
    # symbol when present; the ticker prefix is the fallback derivation.
    market_symbol = normalize_ticker(instrument.short_name) or normalize_ticker(symbol) or None

    return {
        "broker": broker,
        "broker_ticker": instrument.ticker,
        "name": instrument.name,
        "short_name": instrument.short_name,
        "isin": normalize_isin(instrument.isin) or None,
        "currency": normalize_currency(instrument.currency_code) or None,
        "instrument_type": instrument.type,
        "extended_hours": instrument.extended_hours,
        "min_trade_quantity": _decimal(instrument.min_trade_quantity),
        "max_open_quantity": _decimal(instrument.max_open_quantity),
        "working_schedule_id": instrument.working_schedule_id,
        "added_on": instrument.added_on,
        "exchange": exchange_name,
        "exchange_id": exchange_id,
        "market_symbol": market_symbol,
        "market_code": market_code,
        "name_key": instrument_name_key(instrument.name) or None,
        "is_active": True,
        "last_seen_at": now,
        "last_refreshed_at": now,
        "raw_metadata": instrument.model_dump(mode="json", by_alias=True),
        "updated_at": now,
    }


class InstrumentSyncService:
    """Refreshes broker instrument and exchange metadata."""

    def __init__(
        self,
        database: Database,
        client: Trading212MetadataClient,
        *,
        broker: Broker = Broker.TRADING212,
    ) -> None:
        self._database = database
        self._client = client
        self._broker = broker

    async def sync(self) -> InstrumentSyncResult:
        """Fetch both metadata endpoints and reconcile them into the database."""
        result = InstrumentSyncResult(broker=self._broker)

        exchanges = await self._client.fetch_exchanges()
        schedule_map = await self._store_exchanges(exchanges, result)

        instruments = await self._client.fetch_instruments()
        result.instruments_seen = len(instruments)
        await self._store_instruments(instruments, schedule_map, result)

        result.rate_limit = self._client.rate_limit_snapshot()
        result.finished_at = utcnow()

        METRICS.set(
            "stockbrain_broker_instruments_total",
            float(result.instruments_written),
            labels={"broker": self._broker.value},
        )
        log.info("instrument_sync_complete", **result.as_dict())
        return result

    # ------------------------------------------------------------------
    async def _store_exchanges(
        self, exchanges: list[T212Exchange], result: InstrumentSyncResult
    ) -> dict[int, tuple[int, str | None]]:
        """Upsert exchanges and their schedules; return schedule id -> exchange."""
        now = utcnow()
        schedule_map: dict[int, tuple[int, str | None]] = {}
        if not exchanges:
            return schedule_map

        exchange_rows = [
            {
                "broker": self._broker,
                "provider_exchange_id": exchange.id,
                "name": exchange.name,
                "raw_metadata": exchange.model_dump(mode="json", by_alias=True),
                "last_refreshed_at": now,
                "updated_at": now,
            }
            for exchange in exchanges
        ]

        async with self._database.transaction() as session:
            statement = pg_insert(BrokerExchange).values(exchange_rows)
            upsert = statement.on_conflict_do_update(
                constraint="uq_broker_exchanges_broker_provider_exchange_id",
                set_={
                    "name": statement.excluded.name,
                    "raw_metadata": statement.excluded.raw_metadata,
                    "last_refreshed_at": statement.excluded.last_refreshed_at,
                    "updated_at": statement.excluded.updated_at,
                },
            ).returning(BrokerExchange.id, BrokerExchange.provider_exchange_id)
            rows = (await session.execute(upsert)).all()
            ids_by_provider = {int(provider_id): row_id for row_id, provider_id in rows}
            result.exchanges = len(rows)

            schedule_rows: list[dict[str, Any]] = []
            for exchange in exchanges:
                exchange_uuid = ids_by_provider.get(exchange.id)
                if exchange_uuid is None:  # pragma: no cover - RETURNING covers every row
                    continue
                for schedule in exchange.working_schedules:
                    schedule_map[schedule.id] = (exchange.id, exchange.name)
                    schedule_rows.append(
                        {
                            "broker": self._broker,
                            "provider_schedule_id": schedule.id,
                            "exchange_id": exchange_uuid,
                            "time_events": [
                                event.model_dump(mode="json") for event in schedule.time_events
                            ],
                            "last_refreshed_at": now,
                            "updated_at": now,
                        }
                    )

            for chunk in _chunks(schedule_rows, _BATCH_SIZE):
                schedule_stmt = pg_insert(BrokerWorkingSchedule).values(chunk)
                await session.execute(
                    schedule_stmt.on_conflict_do_update(
                        constraint="uq_broker_working_schedules_broker_provider_schedule_id",
                        set_={
                            "exchange_id": schedule_stmt.excluded.exchange_id,
                            "time_events": schedule_stmt.excluded.time_events,
                            "last_refreshed_at": schedule_stmt.excluded.last_refreshed_at,
                            "updated_at": schedule_stmt.excluded.updated_at,
                        },
                    )
                )
            result.schedules = len(schedule_rows)

        return schedule_map

    async def _store_instruments(
        self,
        instruments: list[T212Instrument],
        schedule_map: dict[int, tuple[int, str | None]],
        result: InstrumentSyncResult,
    ) -> None:
        now = utcnow()
        rows = [
            instrument_row(
                instrument, broker=self._broker, exchange_by_schedule=schedule_map, now=now
            )
            for instrument in instruments
        ]
        # A provider that answers with an empty universe is a provider fault, not
        # an instruction to deactivate everything the resolver depends on.
        if not rows:
            log.warning("instrument_sync_empty_response", broker=self._broker.value)
            return

        result.with_exchange = sum(1 for row in rows if row["exchange"])
        result.without_exchange = len(rows) - result.with_exchange

        updatable = (
            "name",
            "short_name",
            "isin",
            "currency",
            "instrument_type",
            "extended_hours",
            "min_trade_quantity",
            "max_open_quantity",
            "working_schedule_id",
            "added_on",
            "exchange",
            "exchange_id",
            "market_symbol",
            "market_code",
            "name_key",
            "is_active",
            "last_seen_at",
            "last_refreshed_at",
            "raw_metadata",
            "updated_at",
        )
        async with self._database.transaction() as session:
            for chunk in _chunks(rows, _BATCH_SIZE):
                statement = pg_insert(BrokerInstrument).values(chunk)
                await session.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_broker_instruments_broker_broker_ticker",
                        set_={name: getattr(statement.excluded, name) for name in updatable},
                    )
                )
            result.instruments_written = len(rows)

            # Anything not refreshed by this run is no longer offered. Compared
            # by timestamp rather than by ticker list so the statement stays one
            # UPDATE regardless of universe size, and so a concurrent sync that
            # finished later cannot deactivate rows it just wrote.
            deactivated = await session.execute(
                sa.update(BrokerInstrument)
                .where(
                    BrokerInstrument.broker == self._broker,
                    BrokerInstrument.is_active.is_(True),
                    sa.or_(
                        BrokerInstrument.last_seen_at.is_(None),
                        BrokerInstrument.last_seen_at < now,
                    ),
                )
                .values(is_active=False, updated_at=now)
                .returning(BrokerInstrument.id)
            )
            result.deactivated = len(list(deactivated.scalars()))


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
