"""Migration tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from stockbrain.db.models import Base
from stockbrain.db.session import Database
from stockbrain.startup import check_schema_current

pytestmark = pytest.mark.integration

EXPECTED_TABLES = set(Base.metadata.tables)


async def test_migration_creates_every_mapped_table(database: Database) -> None:
    async with database.session() as session:
        result = await session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        present = {row[0] for row in result}
    missing = EXPECTED_TABLES - present
    assert not missing, f"tables missing from the migration: {sorted(missing)}"


async def test_schema_reports_as_current_after_migration(database: Database) -> None:
    current, detail = await check_schema_current(database)
    assert current, detail


async def test_enum_types_exist_natively(database: Database) -> None:
    async with database.session() as session:
        result = await session.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public'"
            )
        )
        types = {row[0] for row in result}
    for expected in ("proposal_status", "execution_outcome", "job_status", "provider_status"):
        assert expected in types


async def test_phase_four_enum_types_exist(database: Database) -> None:
    async with database.session() as session:
        result = await session.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public'"
            )
        )
        types = {row[0] for row in result}
    for expected in ("resolution_status", "alias_type"):
        assert expected in types


async def test_phase_four_uniqueness_indexes_exist(database: Database) -> None:
    """The constraints that make Phase 4's guarantees database-level.

    Each of these turns a hopeful application check into something a bug, a race
    or a future refactor cannot bypass.
    """
    async with database.session() as session:
        result = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
        indexes = {row[0] for row in result}
    for expected in (
        "uq_broker_instruments_broker_broker_ticker",
        "uq_broker_exchanges_broker_provider_exchange_id",
        "uq_broker_working_schedules_broker_provider_schedule_id",
        "uq_company_aliases_authoritative_scope",
        "uq_companies_isin",
    ):
        assert expected in indexes, f"{expected} is missing"
