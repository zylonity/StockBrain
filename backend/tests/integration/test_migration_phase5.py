"""Phase 5 migration preserves legacy research and round-trips the full schema."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def test_full_roundtrip_and_alembic_drift(migrated_database: str) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")


async def legacy_row(url: str, *, seed: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if seed:
                await conn.execute(
                    text(
                        "INSERT INTO companies (id, name) VALUES "
                        "('11111111-1111-1111-1111-111111111111', 'Legacy company')"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO research_runs (id, company_id, status, "
                        "structured_decision) VALUES "
                        "('22222222-2222-2222-2222-222222222222', "
                        "'11111111-1111-1111-1111-111111111111', "
                        "'SUCCEEDED', '{\"legacy\": true}'::jsonb)"
                    )
                )
            else:
                row = (
                    await conn.execute(
                        text(
                            "SELECT structured_decision, dedupe_key FROM research_runs "
                            "WHERE id='22222222-2222-2222-2222-222222222222'"
                        )
                    )
                ).one()
                assert row[0] == {"legacy": True} and row[1] is None
    finally:
        await engine.dispose()


def test_legacy_research_survives_phase_five(migrated_database: str) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", migrated_database)
    try:
        command.downgrade(config, "9c31f4b70ad2")
        asyncio.run(legacy_row(migrated_database, seed=True))
        command.upgrade(config, "head")
        asyncio.run(legacy_row(migrated_database, seed=False))
        command.check(config)
    finally:
        command.upgrade(config, "head")
