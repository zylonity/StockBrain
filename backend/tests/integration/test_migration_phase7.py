"""The Phase 7 migration against populated approval actions.

Two things break this migration and neither appears on an empty database:

* ``approval_stage`` gains two values, and PostgreSQL refuses to *use* a new
  enum value in the transaction that added it -- so the ``ALTER TYPE`` runs in
  an autocommit block, and this test proves rows can then be written with the
  new stages;
* the downgrade cannot drop an enum value, so it rebuilds the type. Rows using
  a stage that will not exist afterwards have to go first, or the cast fails and
  the "downgrade" does not actually reverse anything.

The rows deleted on downgrade are ephemeral single-use callback tokens, mostly
already consumed. The audit log keeps the actions that mattered.
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

PREVIOUS = "36f53456be8a"
PHASE_7 = "8c41d0f7ab92"

COMPANY = "22222222-3333-4444-5555-666666666661"
INSTRUMENT = "22222222-3333-4444-5555-666666666662"
PROPOSAL = "22222222-3333-4444-5555-666666666663"
LEGACY_APPROVE = "22222222-3333-4444-5555-666666666664"
LEGACY_CONFIRM = "22222222-3333-4444-5555-666666666665"
NEW_REJECT = "22222222-3333-4444-5555-666666666666"
NEW_DETAILS = "22222222-3333-4444-5555-666666666667"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _seed_legacy(url: str) -> None:
    """A proposal with two pre-Phase-7 approval actions.

    One consumed, one still open, neither carrying a chat binding -- because
    before this revision there was no such column, and back-filling one would
    claim a restriction that was never in force.
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
            await conn.execute(
                text(
                    "INSERT INTO trade_proposals (id, broker, broker_ticker, side, order_type, "
                    "proposed_quantity, reference_price, reference_currency, price_source, "
                    "quote_timestamp, quote_age_ms, estimated_notional, account_currency, "
                    "status, expires_at) VALUES (:id, 'TRADING212', 'LGCY_US_EQ', 'BUY', "
                    "'MARKET', 3, 100.5, 'USD', 'ALPACA_IEX', now(), 120, 301.5, 'USD', "
                    "'READY', now() + interval '1 hour')"
                ),
                {"id": PROPOSAL},
            )
            insert = (
                "INSERT INTO approval_actions (id, proposal_id, channel, stage, "
                "opaque_token_hash, user_identifier, expires_at, consumed_at) VALUES "
                "(:id, :proposal, 'TELEGRAM', :stage, :hash, :user, "
                "now() + interval '10 minutes', :consumed)"
            )
            await conn.execute(
                text(insert),
                {
                    "id": LEGACY_APPROVE,
                    "proposal": PROPOSAL,
                    "stage": "APPROVE",
                    "hash": "a" * 64,
                    "user": "12345",
                    "consumed": dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC),
                },
            )
            await conn.execute(
                text(insert),
                {
                    "id": LEGACY_CONFIRM,
                    "proposal": PROPOSAL,
                    "stage": "CONFIRM",
                    "hash": "b" * 64,
                    "user": "12345",
                    "consumed": None,
                },
            )
    finally:
        await engine.dispose()


async def _assert_upgraded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            # The legacy rows survive with a NULL chat binding.
            rows = {
                str(row[0]): row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, chat_identifier, stage::text, consumed_at "
                            "FROM approval_actions"
                        )
                    )
                ).all()
            }
            assert rows[LEGACY_APPROVE][1] is None, "no chat binding is invented"
            assert rows[LEGACY_CONFIRM][1] is None
            assert rows[LEGACY_APPROVE][3] is not None

            labels = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                            "ON t.oid = e.enumtypid WHERE t.typname = 'approval_stage'"
                        )
                    )
                ).all()
            }
            assert {"APPROVE", "CONFIRM", "REJECT", "DETAILS"} == labels

            # The new values are usable, which is what the autocommit block buys.
            insert = (
                "INSERT INTO approval_actions (id, proposal_id, channel, stage, "
                "opaque_token_hash, user_identifier, chat_identifier, expires_at) VALUES "
                "(:id, :proposal, 'TELEGRAM', :stage, :hash, '12345', '12345', "
                "now() + interval '10 minutes')"
            )
            await conn.execute(
                text(insert),
                {"id": NEW_REJECT, "proposal": PROPOSAL, "stage": "REJECT", "hash": "c" * 64},
            )
            await conn.execute(
                text(insert),
                {"id": NEW_DETAILS, "proposal": PROPOSAL, "stage": "DETAILS", "hash": "d" * 64},
            )

            indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename='approval_actions'")
                    )
                ).all()
            }
            assert "ix_approval_actions_open" in indexes
            assert "uq_approval_actions_opaque_token_hash" in indexes
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
                            "ON t.oid = e.enumtypid WHERE t.typname = 'approval_stage'"
                        )
                    )
                ).all()
            }
            assert labels == {"APPROVE", "CONFIRM"}

            remaining = {
                str(row[0])
                for row in (await conn.execute(text("SELECT id FROM approval_actions"))).all()
            }
            # The two pre-Phase-7 rows survive; the two using a stage that no
            # longer exists are removed rather than recast into "APPROVE".
            assert remaining == {LEGACY_APPROVE, LEGACY_CONFIRM}

            columns = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='approval_actions'"
                        )
                    )
                ).all()
            }
            assert "chat_identifier" not in columns
            assert {"proposal_id", "opaque_token_hash", "user_identifier"} <= columns

            # The proposal itself is untouched by any of this.
            count = (await conn.execute(text("SELECT count(*) FROM trade_proposals"))).scalar_one()
            assert count == 1
    finally:
        await engine.dispose()


def test_the_migration_survives_populated_approval_actions(migrated_database: str) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        asyncio.run(_seed_legacy(migrated_database))
        command.upgrade(config, PHASE_7)
        asyncio.run(_assert_upgraded(migrated_database))

        # Rows using the newly added enum values must not block the downgrade.
        command.downgrade(config, PREVIOUS)
        asyncio.run(_assert_downgraded(migrated_database))
    finally:
        command.upgrade(config, "head")


def test_the_app_settings_row_a_control_flag_uses_predates_this_phase(
    migrated_database: str,
) -> None:
    """The pause and kill switch need no schema change.

    ``app_settings`` has existed since the initial schema and already carries
    ``updated_at`` and ``updated_by``, which is exactly the ``changed_at`` and
    ``actor`` the control state records. Adding a table for two rows would have
    been a migration for its own sake.
    """

    async def check() -> None:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.begin() as conn:
                columns = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_name='app_settings'"
                            )
                        )
                    ).all()
                }
                assert {"key", "value", "updated_at", "updated_by"} <= columns
        finally:
            await engine.dispose()

    asyncio.run(check())
