"""Shared test fixtures.

Integration tests need a real PostgreSQL because several of the system's
safety guarantees are partial unique indexes -- they cannot be exercised
against SQLite or a mock.  They are skipped, not silently passed, when no
database is configured.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from stockbrain.config import Settings
from stockbrain.db.session import Database

BACKEND_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "WARNING",
        "stockbrain_secret_key": "test-secret-key-not-used-in-production",
    }
    if TEST_DATABASE_URL:
        base["database_url"] = TEST_DATABASE_URL
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def make_settings() -> object:
    return _settings


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("DATABASE_URL_TEST (or DATABASE_URL) is not set")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[str]:
    """Rebuild the schema from the Alembic migrations once per session.

    Running the real migrations rather than ``metadata.create_all`` means the
    tests exercise exactly what production will apply.
    """
    config = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    # A database that has never been migrated has no alembic_version table.
    with contextlib.suppress(Exception):
        command.downgrade(config, "base")
    command.upgrade(config, "head")
    try:
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    db = Database(_settings(database_url=migrated_database))
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def clean_tables(database: Database) -> AsyncIterator[Database]:
    """Truncate mutable tables between tests, keeping the schema in place."""
    tables = (
        "execution_attempts",
        "approval_actions",
        "broker_orders",
        "risk_evaluations",
        "trade_proposals",
        "portfolio_snapshots",
        "positions",
        "theses",
        "research_runs",
        "event_company_impacts",
        "event_sources",
        "sources",
        "events",
        "broker_instruments",
        "broker_working_schedules",
        "broker_exchanges",
        "company_aliases",
        "companies",
        "jobs",
        "llm_calls",
        "notifications",
        "audit_log",
        # Durable execution control (pause / kill switch) lives here. Leaving it
        # behind would let one test's emergency stop halt the next one's world.
        "app_settings",
    )
    async with database.transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    yield database


@pytest.fixture
async def clean_llm_calls(database: Database) -> AsyncIterator[Database]:
    """Empty ``llm_calls`` only, for budget tests that drive spend directly."""
    async with database.transaction() as session:
        await session.execute(text("TRUNCATE llm_calls RESTART IDENTITY CASCADE"))
    yield database
