"""The disclosure-feed enum migration against a real database."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

DISCLOSURE = "d47f2a9c81e5"
PREVIOUS = "b7d2e4f1a9c3"
NEW_LABELS = {"INVESTEGATE", "EQS", "CNMV", "GLOBENEWSWIRE", "ACTUSNEWS"}


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _enum_labels(url: str, type_name: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                    "ON t.oid = e.enumtypid WHERE t.typname = :name"
                ),
                {"name": type_name},
            )
            return {str(row[0]) for row in rows}
    finally:
        await engine.dispose()


def test_source_provider_gains_the_disclosure_wire_labels(migrated_database: str) -> None:
    labels = asyncio.run(_enum_labels(migrated_database, "source_provider"))
    assert NEW_LABELS <= labels


def test_the_migration_round_trips(migrated_database: str) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        command.upgrade(config, DISCLOSURE)
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_the_migration_imports_no_application_code() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260921_1200_disclosure_feeds.py"
    ).read_text()
    imports = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]
    assert not any("stockbrain" in line for line in imports), imports
