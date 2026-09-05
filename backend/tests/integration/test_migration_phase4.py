"""Phase 4 migration against a realistically populated table.

A migration that only works on an empty database is untested. These run the
Phase 4 revision against rows that already exist -- including the awkward ones:
a company whose name needs normalising, an instrument whose derived columns must
be backfilled, and two authoritative aliases that already contradict each other.

The migration is driven directly rather than through the app's models, because
what is under test is the migration's own frozen logic.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

#: The revision under test and the one immediately before it.
PHASE_4 = "9c31f4b70ad2"
PHASE_3 = "4e0854e62da5"


@pytest.fixture
def alembic_config(migrated_database: str) -> AlembicConfig:
    config = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database)
    return config


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one async database unit of work from a synchronous test.

    These tests must be synchronous, because Alembic's ``env.py`` calls
    ``asyncio.run`` itself and would fail inside an already-running loop. Only
    asyncpg is installed, so the database work still has to be async; each call
    gets its own short-lived loop.
    """
    return asyncio.run(coroutine)


@pytest.fixture
def at_phase_three(alembic_config: AlembicConfig, migrated_database: str) -> Iterator[str]:
    """Roll back to the pre-Phase-4 schema, and restore afterwards.

    Session-scoped fixtures leave the database at head, so this restores it even
    when the test fails: a later test finding a downgraded schema would fail for
    a reason that has nothing to do with itself.
    """
    command.downgrade(alembic_config, PHASE_3)
    try:
        yield migrated_database
    finally:
        command.upgrade(alembic_config, "head")


def _seed_pre_migration_rows(url: str) -> dict[str, uuid.UUID]:
    """Rows a real deployment would already have when Phase 4 is applied."""
    return _run(_seed(url))


async def _seed(url: str) -> dict[str, uuid.UUID]:
    ids = {
        "apple": uuid.uuid4(),
        "vertiv": uuid.uuid4(),
        "other": uuid.uuid4(),
        "instrument_a": uuid.uuid4(),
        "instrument_b": uuid.uuid4(),
        "alias_first": uuid.uuid4(),
        "alias_second": uuid.uuid4(),
        "alias_third": uuid.uuid4(),
    }
    engine = create_async_engine(url, connect_args={"statement_cache_size": 0})
    async with engine.begin() as connection:
        # These tests share one database and each needs a known starting point.
        await connection.execute(
            text(
                "TRUNCATE event_company_impacts, broker_instruments, company_aliases, "
                "companies RESTART IDENTITY CASCADE"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO companies (id, name, primary_symbol, exchange, isin, aliases, "
                "is_watchlisted, created_at, updated_at) VALUES "
                "(:a, 'Apple Inc.', 'AAPL', 'NASDAQ', 'US0378331005', '{}'::jsonb, true, "
                "now(), now()), "
                "(:v, 'Vertiv Holdings Co.', 'VRT', 'NYSE', 'US92537N1081', '{}'::jsonb, false, "
                "now(), now()), "
                "(:o, 'Ålborg Ünicode Ltd', NULL, NULL, NULL, '{}'::jsonb, false, now(), now())"
            ),
            {"a": ids["apple"], "v": ids["vertiv"], "o": ids["other"]},
        )
        await connection.execute(
            text(
                "INSERT INTO broker_instruments (id, broker, broker_ticker, name, short_name, "
                "isin, currency, instrument_type, extended_hours, raw_metadata, created_at, "
                "updated_at) VALUES "
                "(:a, 'TRADING212', 'AAPL_US_EQ', 'Apple Inc.', 'AAPL', 'US0378331005', 'USD', "
                "'STOCK', true, '{}'::jsonb, now(), now()), "
                "(:b, 'TRADING212', 'LEGACYTICKER', 'Legacy Two-Part', NULL, NULL, 'GBX', "
                "'STOCK', false, '{}'::jsonb, now(), now())"
            ),
            {"a": ids["instrument_a"], "b": ids["instrument_b"]},
        )
        # Two aliases already claiming the same name for different companies.
        await connection.execute(
            text(
                "INSERT INTO company_aliases (id, company_id, alias, alias_normalized, source, "
                "confidence, created_at, updated_at) VALUES "
                "(:one, :apple, 'Big Fruit', 'big fruit', 'MANUAL', 1.0, "
                "  now() - interval '2 days', now()), "
                "(:two, :vertiv, 'Big Fruit', 'big fruit', 'MANUAL', 1.0, "
                "  now() - interval '1 day', now()), "
                "(:three, :vertiv, 'Vertiv', 'vertiv', 'MANUAL', 1.0, now(), now())"
            ),
            {
                "one": ids["alias_first"],
                "two": ids["alias_second"],
                "three": ids["alias_third"],
                "apple": ids["apple"],
                "vertiv": ids["vertiv"],
            },
        )
    await engine.dispose()
    return ids


def test_the_migration_backfills_and_constrains_a_populated_database(
    at_phase_three: str, alembic_config: AlembicConfig
) -> None:
    ids = _seed_pre_migration_rows(at_phase_three)

    command.upgrade(alembic_config, PHASE_4)

    companies, instruments, aliases, impacts_default = _run(_read_state(at_phase_three))

    # Company name keys are backfilled with the frozen normaliser: legal suffix
    # stripped, accents folded, punctuation removed.
    assert companies["Apple Inc."] == "apple"
    assert companies["Vertiv Holdings Co."] == "vertiv"
    assert companies["Ålborg Ünicode Ltd"] == "alborg unicode"

    # Derived instrument identity is backfilled from what already existed.
    apple = instruments["AAPL_US_EQ"]
    assert apple.market_symbol == "AAPL"
    assert apple.market_code == "US"
    assert apple.name_key == "apple"
    assert apple.is_active is True
    # No working-schedule data existed, so the exchange stays unknown rather
    # than being guessed from the ticker.
    assert apple.exchange is None

    # A ticker that does not have the three-part shape yields no market code.
    legacy = instruments["LEGACYTICKER"]
    assert legacy.market_code is None
    assert legacy.market_symbol == "LEGACYTICKER"

    # The older of the two contradicting authoritative aliases survives; the
    # later one is demoted and says why, rather than being deleted.
    assert aliases[ids["alias_first"]].is_authoritative is True
    assert aliases[ids["alias_second"]].is_authoritative is False
    assert "9c31f4b70ad2" in (aliases[ids["alias_second"]].notes or "")
    assert aliases[ids["alias_third"]].is_authoritative is True
    assert aliases[ids["alias_third"]].alias_type == "COMMON"

    # Existing impacts are PENDING: they were never resolved against broker
    # metadata, and inventing a RESOLVED state for an unverified mapping would
    # be exactly the silent fallback this phase forbids.
    assert "PENDING" in impacts_default


async def _read_state(
    url: str,
) -> tuple[dict[str, str], dict[str, Any], dict[uuid.UUID, Any], str]:
    engine = create_async_engine(url, connect_args={"statement_cache_size": 0})
    async with engine.begin() as connection:
        companies = {
            row.name: row.name_key
            for row in (await connection.execute(text("SELECT name, name_key FROM companies")))
        }
        instruments = {
            row.broker_ticker: row
            for row in (
                await connection.execute(
                    text(
                        "SELECT broker_ticker, market_symbol, market_code, name_key, is_active, "
                        "exchange FROM broker_instruments"
                    )
                )
            )
        }
        aliases = {
            row.id: row
            for row in (
                await connection.execute(
                    text("SELECT id, is_authoritative, alias_type, notes FROM company_aliases")
                )
            )
        }
        impacts_default = (
            await connection.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'event_company_impacts' "
                    "AND column_name = 'resolution_status'"
                )
            )
        ).scalar_one()
    await engine.dispose()
    return companies, instruments, aliases, str(impacts_default)


async def _row_counts(url: str, surviving_alias: uuid.UUID) -> tuple[int, int, int, int]:
    engine = create_async_engine(url, connect_args={"statement_cache_size": 0})
    async with engine.begin() as connection:
        companies = (await connection.execute(text("SELECT count(*) FROM companies"))).scalar_one()
        instruments = (
            await connection.execute(text("SELECT count(*) FROM broker_instruments"))
        ).scalar_one()
        aliases = (
            await connection.execute(text("SELECT count(*) FROM company_aliases"))
        ).scalar_one()
        surviving = (
            await connection.execute(
                text("SELECT count(*) FROM company_aliases WHERE id = :id"),
                {"id": surviving_alias},
            )
        ).scalar_one()
    await engine.dispose()
    return int(companies), int(instruments), int(aliases), int(surviving)


async def _insert_contradicting_alias(url: str, company_id: uuid.UUID) -> None:
    engine = create_async_engine(url, connect_args={"statement_cache_size": 0})
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO company_aliases (id, company_id, alias, alias_normalized, "
                    "alias_type, source, confidence, is_authoritative, created_at, updated_at) "
                    "VALUES (:id, :company, 'Big Fruit', 'big fruit', 'COMMON', 'MANUAL', 1.0, "
                    "true, now(), now())"
                ),
                {"id": uuid.uuid4(), "company": company_id},
            )
    finally:
        await engine.dispose()


def test_the_migration_downgrades_and_re_upgrades_with_rows_present(
    at_phase_three: str, alembic_config: AlembicConfig
) -> None:
    """A rollback must not destroy the rows the deployment already had."""
    ids = _seed_pre_migration_rows(at_phase_three)

    command.upgrade(alembic_config, PHASE_4)
    command.downgrade(alembic_config, PHASE_3)
    command.upgrade(alembic_config, PHASE_4)

    companies, instruments, aliases, surviving = _run(
        _row_counts(at_phase_three, ids["alias_first"])
    )

    assert companies == 3
    assert instruments == 2
    assert aliases == 3
    assert surviving == 1


def test_the_authoritative_alias_index_refuses_a_contradiction_after_migration(
    at_phase_three: str, alembic_config: AlembicConfig
) -> None:
    """The demotion is not a one-off clean-up: the index keeps holding."""
    from sqlalchemy.exc import IntegrityError

    ids = _seed_pre_migration_rows(at_phase_three)
    command.upgrade(alembic_config, PHASE_4)

    with pytest.raises(IntegrityError):
        _run(_insert_contradicting_alias(at_phase_three, ids["other"]))
