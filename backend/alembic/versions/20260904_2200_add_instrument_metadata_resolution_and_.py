"""add instrument metadata resolution and alias scoping

Revision ID: 9c31f4b70ad2
Revises: 4e0854e62da5
Create Date: 2026-09-04 22:00:00.000000+00:00

Phase 4 schema.  Three groups of change:

* ``broker_exchanges`` / ``broker_working_schedules`` -- Trading 212 puts no
  exchange on an instrument, only a ``workingScheduleId``, so the schedule map
  is what an instrument's exchange is derived from.
* ``broker_instruments`` gains the derived identity columns instrument
  resolution searches on, plus ``is_active``/``last_seen_at`` so a delisted
  instrument is retired rather than deleted (a proposal may reference it).
* ``event_company_impacts`` gains the resolution verdict, and
  ``company_aliases`` gains listing scope with a partial unique index that makes
  two contradicting authoritative aliases impossible.

Every added column is nullable or carries a server default, every backfill runs
before its constraint, and the normalisation used by the backfill is inlined and
frozen: a migration must keep producing the same values forever, so it must not
import application code that may later change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c31f4b70ad2"
down_revision: str | None = "4e0854e62da5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------
# Frozen copies of stockbrain.instruments.normalize as of this revision.
# --------------------------------------------------------------------------
_NAME_SUFFIXES = frozenset(
    {
        "incorporated",
        "corporation",
        "company",
        "limited",
        "holdings",
        "holding",
        "group",
        "plc",
        "inc",
        "corp",
        "co",
        "ltd",
        "llc",
        "lp",
        "sa",
        "nv",
        "ag",
        "se",
        "spa",
        "oyj",
        "ab",
        "as",
        "asa",
        "adr",
        "ads",
    }
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TICKER_ALLOWED = re.compile(r"[^A-Z0-9.\-]")


def _instrument_name_key(name: str | None) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _NON_ALNUM.sub(" ", text.lower()).strip()
    if not text:
        return ""
    words = text.split()
    while len(words) > 1 and words[-1] in _NAME_SUFFIXES:
        words.pop()
    return " ".join(words)


def _normalize_ticker(ticker: str | None) -> str:
    if not ticker:
        return ""
    return _TICKER_ALLOWED.sub("", ticker.strip().upper())


def _split_broker_ticker(broker_ticker: str) -> tuple[str, str | None]:
    parts = broker_ticker.split("_")
    if len(parts) >= 3:
        return parts[0], parts[-2]
    return broker_ticker, None


_BATCH = 1000


def upgrade() -> None:
    connection = op.get_bind()

    alias_type = postgresql.ENUM(
        "LEGAL", "COMMON", "HISTORICAL", "TICKER", "LISTING", name="alias_type", create_type=False
    )
    resolution_status = postgresql.ENUM(
        "PENDING",
        "RESOLVED",
        "AMBIGUOUS",
        "NOT_FOUND",
        "UNSUPPORTED",
        name="resolution_status",
        create_type=False,
    )
    alias_type.create(connection, checkfirst=True)
    resolution_status.create(connection, checkfirst=True)

    # --- exchanges and working schedules --------------------------------
    op.create_table(
        "broker_exchanges",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "broker",
            postgresql.ENUM("TRADING212", name="broker", create_type=False),
            nullable=False,
        ),
        sa.Column("provider_exchange_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "raw_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broker_exchanges"),
        sa.UniqueConstraint(
            "broker", "provider_exchange_id", name="uq_broker_exchanges_broker_provider_exchange_id"
        ),
    )
    op.create_table(
        "broker_working_schedules",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "broker",
            postgresql.ENUM("TRADING212", name="broker", create_type=False),
            nullable=False,
        ),
        sa.Column("provider_schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "time_events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            ["broker_exchanges.id"],
            name="fk_broker_working_schedules_exchange_id_broker_exchanges",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broker_working_schedules"),
        sa.UniqueConstraint(
            "broker",
            "provider_schedule_id",
            name="uq_broker_working_schedules_broker_provider_schedule_id",
        ),
    )

    # --- companies.name_key ---------------------------------------------
    op.add_column("companies", sa.Column("name_key", sa.Text(), nullable=True))
    _backfill_company_name_keys(connection)
    op.create_index("ix_companies_name_key", "companies", ["name_key"])

    # --- broker_instruments ----------------------------------------------
    for column in (
        sa.Column("exchange", sa.Text(), nullable=True),
        sa.Column("exchange_id", sa.BigInteger(), nullable=True),
        sa.Column("market_symbol", sa.Text(), nullable=True),
        sa.Column("market_code", sa.Text(), nullable=True),
        sa.Column("name_key", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("broker_instruments", column)
    op.add_column(
        "broker_instruments",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    _backfill_instrument_identity(connection)
    op.create_index("ix_broker_instruments_broker_isin", "broker_instruments", ["broker", "isin"])
    op.create_index(
        "ix_broker_instruments_broker_market_symbol",
        "broker_instruments",
        ["broker", "market_symbol"],
    )
    op.create_index(
        "ix_broker_instruments_broker_name_key", "broker_instruments", ["broker", "name_key"]
    )
    op.create_index(
        "ix_broker_instruments_working_schedule_id", "broker_instruments", ["working_schedule_id"]
    )

    # --- company_aliases --------------------------------------------------
    op.add_column(
        "company_aliases",
        sa.Column("alias_type", alias_type, server_default="COMMON", nullable=False),
    )
    op.add_column("company_aliases", sa.Column("exchange", sa.Text(), nullable=True))
    op.add_column("company_aliases", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("company_aliases", sa.Column("isin", sa.String(length=12), nullable=True))
    op.add_column("company_aliases", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "company_aliases",
        sa.Column("is_authoritative", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    # An existing table may already hold two rows claiming the same name for
    # different companies. Demoting the later ones to non-authoritative keeps
    # them visible and inspectable rather than deleting curation work, and lets
    # the unique index be created without a manual clean-up step.
    _demote_contradicting_aliases(connection)
    op.create_index(
        "uq_company_aliases_authoritative_scope",
        "company_aliases",
        [
            sa.text("alias_normalized"),
            sa.text("alias_type"),
            sa.text("coalesce(exchange, '')"),
            sa.text("coalesce(currency, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("is_authoritative"),
    )

    # --- event_company_impacts -------------------------------------------
    op.add_column(
        "event_company_impacts",
        sa.Column("broker_instrument_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "event_company_impacts",
        sa.Column("resolution_status", resolution_status, server_default="PENDING", nullable=False),
    )
    op.add_column(
        "event_company_impacts",
        sa.Column(
            "resolution_alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "event_company_impacts", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_event_company_impacts_broker_instrument_id",
        "event_company_impacts",
        "broker_instruments",
        ["broker_instrument_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_event_company_impacts_resolution_status", "event_company_impacts", ["resolution_status"]
    )
    op.create_index(
        "ix_event_company_impacts_broker_instrument_id",
        "event_company_impacts",
        ["broker_instrument_id"],
    )
    # Rows that already carried a company from an earlier phase are not PENDING
    # work, but they were never resolved against broker metadata either, so they
    # stay PENDING and the sweeper picks them up. Recording that explicitly
    # rather than inventing a RESOLVED state for an unverified mapping.


def _backfill_company_name_keys(connection: sa.Connection) -> None:
    while True:
        rows = connection.execute(
            sa.text("SELECT id, name FROM companies WHERE name_key IS NULL LIMIT :limit"),
            {"limit": _BATCH},
        ).fetchall()
        if not rows:
            break
        connection.execute(
            sa.text("UPDATE companies SET name_key = :key WHERE id = :id"),
            [{"id": row.id, "key": _instrument_name_key(row.name) or ""} for row in rows],
        )


def _backfill_instrument_identity(connection: sa.Connection) -> None:
    while True:
        rows = connection.execute(
            sa.text(
                "SELECT id, broker_ticker, short_name, name FROM broker_instruments "
                "WHERE market_symbol IS NULL AND market_code IS NULL AND name_key IS NULL "
                "LIMIT :limit"
            ),
            {"limit": _BATCH},
        ).fetchall()
        if not rows:
            break
        payload = []
        for row in rows:
            symbol, market_code = _split_broker_ticker(row.broker_ticker)
            market_symbol = _normalize_ticker(row.short_name) or _normalize_ticker(symbol)
            payload.append(
                {
                    "id": row.id,
                    "market_symbol": market_symbol or None,
                    "market_code": market_code,
                    "name_key": _instrument_name_key(row.name) or "",
                }
            )
        connection.execute(
            sa.text(
                "UPDATE broker_instruments SET market_symbol = :market_symbol, "
                "market_code = :market_code, name_key = :name_key WHERE id = :id"
            ),
            payload,
        )


def _demote_contradicting_aliases(connection: sa.Connection) -> None:
    """Keep the oldest authoritative claim on each scope; demote the rest."""
    connection.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY alias_normalized, alias_type,
                                        coalesce(exchange, ''), coalesce(currency, '')
                           ORDER BY created_at ASC, id ASC
                       ) AS position
                FROM company_aliases
                WHERE is_authoritative
            )
            UPDATE company_aliases
            SET is_authoritative = false,
                notes = coalesce(notes || ' | ', '')
                        || 'demoted by migration 9c31f4b70ad2: another authoritative alias '
                        || 'already claimed this scope'
            FROM ranked
            WHERE company_aliases.id = ranked.id AND ranked.position > 1
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_company_impacts_broker_instrument_id", table_name="event_company_impacts"
    )
    op.drop_index("ix_event_company_impacts_resolution_status", table_name="event_company_impacts")
    op.drop_constraint(
        "fk_event_company_impacts_broker_instrument_id",
        "event_company_impacts",
        type_="foreignkey",
    )
    op.drop_column("event_company_impacts", "resolved_at")
    op.drop_column("event_company_impacts", "resolution_alternatives")
    op.drop_column("event_company_impacts", "resolution_status")
    op.drop_column("event_company_impacts", "broker_instrument_id")

    op.drop_index("uq_company_aliases_authoritative_scope", table_name="company_aliases")
    for column in ("is_authoritative", "notes", "isin", "currency", "exchange", "alias_type"):
        op.drop_column("company_aliases", column)

    op.drop_index("ix_broker_instruments_working_schedule_id", table_name="broker_instruments")
    op.drop_index("ix_broker_instruments_broker_name_key", table_name="broker_instruments")
    op.drop_index("ix_broker_instruments_broker_market_symbol", table_name="broker_instruments")
    op.drop_index("ix_broker_instruments_broker_isin", table_name="broker_instruments")
    for column in (
        "is_active",
        "last_seen_at",
        "name_key",
        "market_code",
        "market_symbol",
        "exchange_id",
        "exchange",
    ):
        op.drop_column("broker_instruments", column)

    op.drop_index("ix_companies_name_key", table_name="companies")
    op.drop_column("companies", "name_key")

    op.drop_table("broker_working_schedules")
    op.drop_table("broker_exchanges")

    connection = op.get_bind()
    postgresql.ENUM(name="resolution_status").drop(connection, checkfirst=True)
    postgresql.ENUM(name="alias_type").drop(connection, checkfirst=True)
