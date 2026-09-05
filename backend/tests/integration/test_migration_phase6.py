"""The Phase 6 migration against realistic populated legacy rows.

Two things break a migration against a real database and neither shows up on an
empty one:

* a check constraint added over rows that predate the columns it constrains --
  here, an ``APPROVED`` proposal with no authorization provenance;
* an enum value added and then *used* in the same transaction, which PostgreSQL
  refuses outright.

Both are exercised below, along with the full downgrade, which has to rebuild
the ``proposal_status`` type because an enum value cannot be dropped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

PREVIOUS = "5a180cf497b2"
PHASE_6 = "36f53456be8a"

COMPANY = "11111111-2222-3333-4444-555555555551"
INSTRUMENT = "11111111-2222-3333-4444-555555555552"
APPROVED_WEB = "11111111-2222-3333-4444-555555555553"
APPROVED_TELEGRAM = "11111111-2222-3333-4444-555555555554"
APPROVED_ORPHAN = "11111111-2222-3333-4444-555555555555"
DRAFT = "11111111-2222-3333-4444-555555555556"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _seed_legacy(url: str) -> None:
    """Rows as they would look before Phase 6 ever ran.

    Three approved proposals with different amounts of evidence about who
    approved them, and one draft. The orphan is the interesting one: it has a
    status but no channel, no approver and no timestamp, which is exactly the
    row a naive constraint would reject.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO companies (id, name) VALUES (:id, 'Legacy Corp')"),
                {"id": COMPANY},
            )
            await conn.execute(
                text(
                    "INSERT INTO broker_instruments (id, company_id, broker, broker_ticker) "
                    "VALUES (:id, :company, 'TRADING212', 'LGCY_US_EQ')"
                ),
                {"id": INSTRUMENT, "company": COMPANY},
            )
            base = (
                "INSERT INTO trade_proposals (id, broker, broker_ticker, side, order_type, "
                "proposed_quantity, reference_price, reference_currency, price_source, "
                "quote_timestamp, quote_age_ms, estimated_notional, account_currency, status, "
                "expires_at, approved_at, approved_by, approved_channel) VALUES "
                "(:id, 'TRADING212', :ticker, 'BUY', 'MARKET', 3, 100.5, 'USD', 'ALPACA_IEX', "
                "now(), 120, 301.5, 'USD', :status, now() + interval '1 hour', "
                ":approved_at, :approved_by, :channel)"
            )
            await conn.execute(
                text(base),
                {
                    "id": APPROVED_WEB,
                    "ticker": "WEB_US_EQ",
                    "status": "APPROVED",
                    "approved_at": dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC),
                    "approved_by": "operator@example",
                    "channel": "WEB",
                },
            )
            await conn.execute(
                text(base),
                {
                    "id": APPROVED_TELEGRAM,
                    "ticker": "TG_US_EQ",
                    "status": "APPROVED",
                    "approved_at": dt.datetime(2026, 8, 1, 11, 0, tzinfo=dt.UTC),
                    "approved_by": "12345",
                    "channel": "TELEGRAM",
                },
            )
            await conn.execute(
                text(base),
                {
                    "id": APPROVED_ORPHAN,
                    "ticker": "ORPH_US_EQ",
                    "status": "APPROVED",
                    "approved_at": None,
                    "approved_by": None,
                    "channel": None,
                },
            )
            await conn.execute(
                text(base),
                {
                    "id": DRAFT,
                    "ticker": "DRFT_US_EQ",
                    "status": "DRAFT",
                    "approved_at": None,
                    "approved_by": None,
                    "channel": None,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO portfolio_snapshots (id, broker, currency, cash_available, "
                    "total_value) VALUES "
                    "('11111111-2222-3333-4444-555555555557', 'TRADING212', 'GBP', 100, 200)"
                )
            )
    finally:
        await engine.dispose()


async def _assert_backfilled(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = {
                str(row[0]): row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, authorization_source, approved_by, approved_at, "
                            "execution_policy, status FROM trade_proposals"
                        )
                    )
                ).all()
            }
            assert rows[APPROVED_WEB][1] == "HUMAN_WEB"
            assert rows[APPROVED_TELEGRAM][1] == "HUMAN_TELEGRAM"

            # No evidence of a person, so provenance is reconstructed and
            # labelled rather than attributed to someone who never clicked.
            orphan = rows[APPROVED_ORPHAN]
            assert orphan[1] == "HUMAN_WEB"
            assert orphan[2] == "legacy:unknown"
            assert orphan[3] is not None

            # An unapproved proposal gains no provenance at all.
            assert rows[DRAFT][1] is None
            assert rows[DRAFT][2] is None

            # Every legacy row defaults to the manual policy, so none of them
            # can later be authorized by the system.
            assert all(row[4] == "MANUAL" for row in rows.values())

            snapshot = (
                await conn.execute(
                    text(
                        "SELECT cash_reserved, cash_in_pies, broker_environment "
                        "FROM portfolio_snapshots"
                    )
                )
            ).one()
            assert snapshot == (None, None, None), "no account fact is invented"
    finally:
        await engine.dispose()


async def _assert_downgraded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            labels = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                            "ON t.oid = e.enumtypid WHERE t.typname = 'proposal_status'"
                        )
                    )
                ).all()
            }
            assert "INVALIDATED" not in labels, "the rebuilt type drops the added value"
            assert {"DRAFT", "READY", "APPROVED", "EXECUTED"} <= labels

            # The rows survive, and the indexes that depend on `status` were
            # recreated around the type swap.
            count = (await conn.execute(text("SELECT count(*) FROM trade_proposals"))).scalar_one()
            assert count == 4
            indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename='trade_proposals'")
                    )
                ).all()
            }
            assert "uq_trade_proposals_active_instrument" in indexes
            assert "ix_trade_proposals_status_expires" in indexes
    finally:
        await engine.dispose()


async def _mark_invalidated(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE trade_proposals SET status='INVALIDATED', invalidated_at=now(), "
                    "invalidation_reason='listing retired' WHERE id=:id"
                ),
                {"id": DRAFT},
            )
    finally:
        await engine.dispose()


def test_the_migration_survives_populated_legacy_proposals(migrated_database: str) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        asyncio.run(_seed_legacy(migrated_database))
        command.upgrade(config, PHASE_6)
        asyncio.run(_assert_backfilled(migrated_database))

        # A row using the newly added enum value must not block the downgrade.
        asyncio.run(_mark_invalidated(migrated_database))
        command.downgrade(config, PREVIOUS)
        asyncio.run(_assert_downgraded(migrated_database))
    finally:
        command.upgrade(config, "head")


def test_full_roundtrip_and_alembic_drift(migrated_database: str) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")
