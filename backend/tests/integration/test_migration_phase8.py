"""The Phase 8 migration against populated execution attempts.

Three things break this migration and none appears on an empty database:

1. **The composite foreign key** ``(proposal_id, broker_environment)`` needs
   every existing attempt to already agree with its proposal.  A legacy row that
   disagreed would make the constraint un-addable, so the upgrade backfills from
   the parent -- which is the source of truth -- and deletes nothing.
2. **``ambiguous = (outcome = 'AMBIGUOUS')``** could be violated by a legacy row
   where the boolean and the enum disagreed.  The enum is the authority, so the
   flag is normalised from it before the constraint lands.
3. **The downgrade must actually reverse.**  The check constraints are dropped
   by their *bare* names, because the metadata naming convention prefixes them
   again -- passing the rendered name gets it prefixed twice and then truncated
   with a hash suffix, which is Phase 6's bug #13 in a different hat.
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

PREVIOUS = "8c41d0f7ab92"
PHASE_8 = "5f2a7c93e410"

COMPANY = "33333333-4444-5555-6666-777777777771"
INSTRUMENT = "33333333-4444-5555-6666-777777777772"
PROPOSAL = "33333333-4444-5555-6666-777777777773"
CONSISTENT = "33333333-4444-5555-6666-777777777774"
MISMATCHED = "33333333-4444-5555-6666-777777777775"
FLAG_DISAGREES = "33333333-4444-5555-6666-777777777776"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _seed_legacy(url: str) -> None:
    """A demo proposal with three pre-Phase-8 attempts.

    One consistent, one whose ``broker_environment`` disagrees with its parent,
    and one whose ``ambiguous`` flag contradicts its outcome.  All three are
    shapes the schema permitted before this revision.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO companies (id, name) VALUES (:id, 'Phase 8 Legacy Corp')"),
                {"id": COMPANY},
            )
            await conn.execute(
                text(
                    "INSERT INTO broker_instruments (id, company_id, broker, broker_ticker) "
                    "VALUES (:id, :company, 'TRADING212', 'PH8LGCY_US_EQ')"
                ),
                {"id": INSTRUMENT, "company": COMPANY},
            )
            await conn.execute(
                text(
                    "INSERT INTO trade_proposals (id, broker, broker_ticker, broker_environment, "
                    "side, order_type, proposed_quantity, reference_price, reference_currency, "
                    "price_source, quote_timestamp, quote_age_ms, estimated_notional, "
                    "account_currency, status, expires_at) VALUES (:id, 'TRADING212', "
                    "'PH8LGCY_US_EQ', 'demo', 'BUY', 'MARKET', 3, 100.5, 'USD', 'ALPACA_IEX', "
                    "now(), 120, 301.5, 'USD', 'EXECUTION_AMBIGUOUS', now() + interval '1 hour')"
                ),
                {"id": PROPOSAL},
            )
            insert = (
                "INSERT INTO execution_attempts (id, proposal_id, attempt_number, "
                "broker_environment, request_payload, request_fingerprint, sent_to_broker, "
                "sent_at, outcome, ambiguous) VALUES (:id, :proposal, :number, :environment, "
                "'{}'::jsonb, :fingerprint, :sent, :sent_at, :outcome, :ambiguous)"
            )
            await conn.execute(
                text(insert),
                {
                    "id": CONSISTENT,
                    "proposal": PROPOSAL,
                    "number": 1,
                    "environment": "demo",
                    "fingerprint": "a" * 64,
                    "sent": False,
                    "sent_at": None,
                    "outcome": "FAILED_BEFORE_SEND",
                    "ambiguous": False,
                },
            )
            await conn.execute(
                text(insert),
                {
                    "id": MISMATCHED,
                    "proposal": PROPOSAL,
                    "number": 2,
                    # Wrong: the parent is demo. The upgrade must correct this
                    # rather than fail or delete the row.
                    "environment": "live",
                    "fingerprint": "b" * 64,
                    "sent": False,
                    "sent_at": None,
                    "outcome": "FAILED_BEFORE_SEND",
                    "ambiguous": False,
                },
            )
            await conn.execute(
                text(insert),
                {
                    "id": FLAG_DISAGREES,
                    "proposal": PROPOSAL,
                    "number": 3,
                    "environment": "demo",
                    "fingerprint": "c" * 64,
                    "sent": True,
                    "sent_at": dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC),
                    "outcome": "AMBIGUOUS",
                    # Wrong: the enum says ambiguous, the flag says otherwise.
                    "ambiguous": False,
                },
            )
    finally:
        await engine.dispose()


async def _assert_upgraded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = {
                str(row[0]): row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, broker_environment, ambiguous, outcome::text, "
                            "execution_snapshot, reconciliation_attempts, "
                            "reconciliation_detail, error_category, preflight_at "
                            "FROM execution_attempts WHERE proposal_id = :p"
                        ),
                        {"p": PROPOSAL},
                    )
                ).all()
            }
            # Scoped to the seeded proposal: this test shares a session database
            # with the rest of the suite, so a global count would depend on
            # which file ran last.
            assert len(rows) == 3, "no attempt is deleted"

            # Backfilled from the parent, not guessed and not dropped.
            assert rows[MISMATCHED][1] == "demo"
            assert rows[CONSISTENT][1] == "demo"

            # Normalised from the enum, which is the authority.
            assert rows[FLAG_DISAGREES][2] is True
            assert rows[FLAG_DISAGREES][3] == "AMBIGUOUS"
            assert rows[CONSISTENT][2] is False

            # New columns arrive with defaults, inventing nothing.
            for row in rows.values():
                assert row[4] == {}, "no snapshot is fabricated for a legacy attempt"
                assert row[5] == 0
                assert row[6] == {}
                assert row[7] is None
                assert row[8] is None

            constraints = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE conrelid = "
                            "'execution_attempts'::regclass"
                        )
                    )
                ).all()
            }
            assert "ck_execution_attempts_ambiguous_matches_outcome" in constraints
            assert "ck_execution_attempts_broker_order_requires_send" in constraints
            assert "ck_execution_attempts_broker_outcome_requires_send" in constraints
            assert "fk_execution_attempts_proposal_environment" in constraints

            indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE tablename='execution_attempts'"
                        )
                    )
                ).all()
            }
            assert "ix_execution_attempts_unresolved" in indexes
            assert "ix_execution_attempts_broker_order_id" in indexes
            # The Phase 6 guarantee is untouched.
            assert "uq_execution_attempts_sent_once" in indexes

            order_columns = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='broker_orders'"
                        )
                    )
                ).all()
            }
            assert {"broker_environment", "initiated_from"} <= order_columns
    finally:
        await engine.dispose()


async def _assert_constraints_bite(url: str) -> None:
    """The constraints are not decorative."""
    engine = create_async_engine(url)
    try:
        # An attempt in the other environment cannot exist.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="fk_execution_attempts_proposal_environment"):
                await conn.execute(
                    text(
                        "INSERT INTO execution_attempts (id, proposal_id, attempt_number, "
                        "broker_environment, request_payload, request_fingerprint, "
                        "sent_to_broker, outcome, ambiguous) VALUES (gen_random_uuid(), :p, "
                        "90, 'live', '{}'::jsonb, 'x', false, 'FAILED_BEFORE_SEND', false)"
                    ),
                    {"p": PROPOSAL},
                )
        # The flag and the enum cannot disagree.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="ambiguous_matches_outcome"):
                await conn.execute(
                    text(
                        "INSERT INTO execution_attempts (id, proposal_id, attempt_number, "
                        "broker_environment, request_payload, request_fingerprint, "
                        "sent_to_broker, outcome, ambiguous) VALUES (gen_random_uuid(), :p, "
                        "91, 'demo', '{}'::jsonb, 'y', false, 'FAILED_BEFORE_SEND', true)"
                    ),
                    {"p": PROPOSAL},
                )
        # A broker order id requires a recorded send.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="broker_order_requires_send"):
                await conn.execute(
                    text(
                        "INSERT INTO execution_attempts (id, proposal_id, attempt_number, "
                        "broker_environment, request_payload, request_fingerprint, "
                        "sent_to_broker, outcome, ambiguous, broker_order_id) VALUES "
                        "(gen_random_uuid(), :p, 92, 'demo', '{}'::jsonb, 'z', false, "
                        "'FAILED_BEFORE_SEND', false, '123')"
                    ),
                    {"p": PROPOSAL},
                )
    finally:
        await engine.dispose()


async def _assert_downgraded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            columns = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='execution_attempts'"
                        )
                    )
                ).all()
            }
            for removed in (
                "execution_snapshot",
                "preflight_at",
                "error_category",
                "reconciled_at",
                "reconciliation_result",
                "reconciliation_attempts",
                "reconciliation_detail",
            ):
                assert removed not in columns
            # The Phase 6 columns survive.
            assert {"sent_to_broker", "sent_at", "request_fingerprint"} <= columns

            constraints = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE conrelid = "
                            "'execution_attempts'::regclass"
                        )
                    )
                ).all()
            }
            assert "fk_execution_attempts_proposal_environment" not in constraints
            assert "ck_execution_attempts_ambiguous_matches_outcome" not in constraints
            # No double-prefixed leftover from a mis-named drop.
            assert not any(name.startswith("ck_execution_attempts_ck_") for name in constraints)

            # Every attempt survives the downgrade.
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM execution_attempts WHERE proposal_id = :p"),
                    {"p": PROPOSAL},
                )
            ).scalar_one()
            assert count == 3
    finally:
        await engine.dispose()


def test_the_migration_survives_populated_execution_attempts(
    migrated_database: str,
) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        asyncio.run(_seed_legacy(migrated_database))
        command.upgrade(config, PHASE_8)
        asyncio.run(_assert_upgraded(migrated_database))
        asyncio.run(_assert_constraints_bite(migrated_database))

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
