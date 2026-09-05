"""The Phase 9 migrations against populated tables.

The rule this repository follows: *migrations must survive populated tables.*
Add nullable → backfill inside the migration → constrain, with every literal
frozen at the revision and no import of mutable application logic.

Three backfills here are load-bearing, and a plain ``NULL`` would have got each
of them wrong:

1. ``discovery_queries.next_eligible_at`` — read as "run immediately", a NULL
   would enqueue every existing query on the first sweep after deployment. That
   is a smaller copy of the incident the migration exists because of.
2. ``sources.content_fetched_at`` — the rows that already carry scraped markdown
   have their body. Offering them for a paid content fetch would be the
   migration itself spending money.
3. ``trade_proposals.estimated_notional_account_currency`` — every pre-Phase-9
   proposal was produced under the same-currency rule, so the copy is *exactly*
   correct rather than approximately.
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

PREVIOUS = "5f2a7c93e410"
FIRECRAWL_LEDGER = "9a1c4d2b7e31"
FX_PROVENANCE = "b47e0c81f5a2"

TOPIC = "99999999-1111-2222-3333-000000000001"
QUERY = "99999999-1111-2222-3333-000000000002"
SCRAPED_SOURCE = "99999999-1111-2222-3333-000000000003"
METADATA_SOURCE = "99999999-1111-2222-3333-000000000004"
ALPACA_SOURCE = "99999999-1111-2222-3333-000000000005"
PROPOSAL = "99999999-1111-2222-3333-000000000006"
DOOMED_TOPIC = "99999999-1111-2222-3333-000000000007"
DOOMED_QUERY = "99999999-1111-2222-3333-000000000008"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


# ---------------------------------------------------------------------------
# Seeding a pre-Phase-9 world
# ---------------------------------------------------------------------------
async def _seed_legacy(url: str) -> None:
    """A discovery query mid-cadence, three sources, and one live proposal.

    All four shapes the schema permitted at revision ``5f2a7c93e410``.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO discovery_topics "
                    "(id, slug, name, enabled, interval_minutes, result_limit, freshness, "
                    " include_domains, exclude_domains, created_at, updated_at) "
                    "VALUES (:id, 'legacy_ai', 'Legacy AI', true, 20, 10, 'qdr:h', "
                    " '[]'::jsonb, '[]'::jsonb, now(), now())"
                ),
                {"id": TOPIC},
            )
            # Ran 30 minutes ago on the old 20-minute cadence, with real
            # counters. Both counters must survive: they are the only record of
            # what the incident cost.
            await conn.execute(
                text(
                    "INSERT INTO discovery_queries "
                    "(id, topic_id, query, enabled, last_run_at, last_success_at, "
                    " consecutive_failures, results_seen, credits_used, created_at, updated_at) "
                    "VALUES (:id, :topic, '\"AI data center\" investment announcement', true, "
                    " now() - interval '30 minutes', now() - interval '30 minutes', "
                    " 0, 42, 900, now(), now())"
                ),
                {"id": QUERY, "topic": TOPIC},
            )

            insert_source = (
                "INSERT INTO sources "
                "(id, provider, canonical_url, original_url, headline, content_hash, "
                " received_at, raw_content, metadata, created_at, updated_at) "
                "VALUES (:id, :provider, :url, :url, 'A headline', :hash, now(), "
                "        :body, jsonb_build_object('has_scraped_markdown', CAST(:md AS boolean)), "
                "        now(), now())"
            )
            await conn.execute(
                text(insert_source),
                {
                    "id": SCRAPED_SOURCE,
                    "provider": "FIRECRAWL",
                    "url": "https://reuters.com/legacy-scraped",
                    "hash": "a" * 64,
                    "body": "# A full scraped article body",
                    "md": True,
                },
            )
            await conn.execute(
                text(insert_source),
                {
                    "id": METADATA_SOURCE,
                    "provider": "FIRECRAWL",
                    "url": "https://cnbc.com/legacy-snippet",
                    "hash": "b" * 64,
                    "body": "A short snippet.",
                    "md": False,
                },
            )
            await conn.execute(
                text(insert_source),
                {
                    "id": ALPACA_SOURCE,
                    "provider": "ALPACA",
                    "url": "https://alpaca.example/article",
                    "hash": "c" * 64,
                    "body": "An article that arrived with its body.",
                    "md": False,
                },
            )

            await conn.execute(
                text(
                    "INSERT INTO trade_proposals "
                    "(id, broker, broker_ticker, account_id, broker_environment, side, "
                    " order_type, proposed_quantity, reference_price, reference_currency, "
                    " price_source, quote_timestamp, quote_age_ms, estimated_notional, "
                    " account_currency, status, expires_at, version, created_at, updated_at, "
                    " risk_snapshot, risk_rules, sizing_reasons, "
                    " authorization_policy_snapshot, execution_policy) "
                    "VALUES (:id, 'TRADING212', 'AAPL_US_EQ', '12345', 'demo', 'BUY', "
                    " 'MARKET', 3, 200.05, 'USD', 'ALPACA_IEX', now(), 250, 600.15, "
                    # READY, not APPROVED: `ck_trade_proposals_approved_requires_
                    # authorization_provenance` demands an actor and an
                    # `approved_at` for an APPROVED row, and inventing them
                    # would be fabricating an authorization to test a backfill.
                    " 'USD', 'READY', now() + interval '30 minutes', 1, now(), now(), "
                    " '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, 'MANUAL')"
                ),
                {"id": PROPOSAL},
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Assertions after the upgrade
# ---------------------------------------------------------------------------
async def _assert_cadence_backfilled(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT next_eligible_at, searches_performed, results_seen, "
                        "       credits_used, next_eligible_at > now() AS waiting "
                        "FROM discovery_queries WHERE id = :id"
                    ),
                    {"id": QUERY},
                )
            ).one()

            # The property that stops the deployment becoming its own incident.
            assert row.next_eligible_at is not None
            assert row.waiting is True, (
                "a query that ran 30 minutes ago must not be immediately due under "
                "the new 720-minute floor"
            )
            # Existing counters untouched.
            assert row.results_seen == 42
            assert row.credits_used == 900
            assert row.searches_performed == 0
    finally:
        await engine.dispose()


async def _assert_content_marker_backfilled(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            fetched = {
                str(row[0]): row[1]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, content_fetched_at IS NOT NULL FROM sources "
                            "WHERE id IN (:a, :b, :c)"
                        ),
                        {"a": SCRAPED_SOURCE, "b": METADATA_SOURCE, "c": ALPACA_SOURCE},
                    )
                ).all()
            }
            # 117 of the 143 real Firecrawl rows are in this state; paying to
            # fetch their bodies again would be the migration spending money.
            assert fetched[SCRAPED_SOURCE] is True
            assert fetched[METADATA_SOURCE] is False
            # An Alpaca row arrived with its body and is not Firecrawl's to
            # re-fetch. The partial index is keyed on the provider for this.
            assert fetched[ALPACA_SOURCE] is False

            definition = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'ix_sources_unfetched_content'"
                    )
                )
            ).scalar_one()
            # The sweep runs on a schedule; without the predicate the index
            # would not be used and it would scan every source ever ingested.
            assert "content_fetched_at IS NULL" in definition
    finally:
        await engine.dispose()


async def _assert_ledger_shape(url: str) -> None:
    """The ledger's defaults and its check constraints, exercised."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO firecrawl_calls "
                    "(id, kind, requested_sources, credits_reserved, credits_charged) "
                    "VALUES (gen_random_uuid(), 'SCRAPE', '[]'::jsonb, 1, 1)"
                )
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT outcome::text, reserved_at, pages_scraped, scrape_requested "
                        "FROM firecrawl_calls"
                    )
                )
            ).one()
            assert row[0] == "RESERVED"
            assert row[2] == 0
            assert row[3] is False
            # `reserved_at` defaults to the DATABASE clock. The budget's day and
            # month windows are computed from the same clock, so a container
            # with a skewed system time cannot widen its own window.
            assert abs((dt.datetime.now(dt.UTC) - row[1]).total_seconds()) < 120

        async with engine.begin() as conn:
            # A negative charge would *increase* the remaining budget. The
            # constraint name is asserted in full on purpose: the migration
            # originally passed an already-rendered `ck_firecrawl_calls_...`
            # name and the metadata convention prefixed it *again*, producing
            # `ck_firecrawl_calls_ck_firecrawl_calls_credits_charged_n_4fc8` --
            # a name no later migration could drop and one that disagreed with
            # the model. Phase 6's bug 13 and Phase 8's bug 18 for a third time,
            # and this is the assertion that caught it.
            with pytest.raises(Exception, match="ck_firecrawl_calls_credits_charged_non_negative"):
                await conn.execute(
                    text(
                        "INSERT INTO firecrawl_calls "
                        "(id, kind, requested_sources, credits_reserved, credits_charged) "
                        "VALUES (gen_random_uuid(), 'SEARCH', '[]'::jsonb, -1, -1)"
                    )
                )

        async with engine.begin() as conn:
            # Every constraint carries the convention's prefix exactly once.
            names = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'firecrawl_calls'::regclass"
                        )
                    )
                ).all()
            }
            assert "ck_firecrawl_calls_credits_reserved_non_negative" in names
            assert "ck_firecrawl_calls_credits_charged_non_negative" in names
            assert "ck_firecrawl_calls_pages_scraped_non_negative" in names
            assert not any(name.startswith("ck_firecrawl_calls_ck_") for name in names), (
                "a double-prefixed, hash-truncated constraint name is one no later "
                "migration can drop"
            )

        async with engine.begin() as conn:
            # `ON DELETE SET NULL`: the spend record outlives its cause.
            # Deleting a topic must not delete the evidence of what it cost.
            #
            # A throwaway topic rather than the seeded one, because
            # `discovery_queries.topic_id` cascades -- deleting the seeded topic
            # would take its query with it, and the downgrade assertions below
            # would then be measuring this test's own cleanup.
            await conn.execute(
                text(
                    "INSERT INTO discovery_topics "
                    "(id, slug, name, enabled, interval_minutes, result_limit, freshness, "
                    " include_domains, exclude_domains, created_at, updated_at) "
                    "VALUES (:id, 'doomed', 'Doomed', true, 720, 5, 'qdr:d', "
                    " '[]'::jsonb, '[]'::jsonb, now(), now())"
                ),
                {"id": DOOMED_TOPIC},
            )
            await conn.execute(
                text(
                    "INSERT INTO discovery_queries "
                    "(id, topic_id, query, enabled, consecutive_failures, results_seen, "
                    " credits_used, searches_performed, created_at, updated_at) "
                    "VALUES (:id, :topic, 'doomed query', true, 0, 0, 0, 0, now(), now())"
                ),
                {"id": DOOMED_QUERY, "topic": DOOMED_TOPIC},
            )
            await conn.execute(
                text(
                    "INSERT INTO firecrawl_calls "
                    "(id, kind, outcome, query_id, topic_slug, requested_sources, "
                    " credits_reserved, credits_charged) "
                    "VALUES (gen_random_uuid(), 'SEARCH', 'SUCCEEDED', :q, 'doomed', "
                    " '[\"web\"]'::jsonb, 2, 2)"
                ),
                {"q": DOOMED_QUERY},
            )
            await conn.execute(
                text("DELETE FROM discovery_topics WHERE id = :id"), {"id": DOOMED_TOPIC}
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT query_id, topic_slug, credits_charged FROM firecrawl_calls "
                        "WHERE topic_slug = 'doomed'"
                    )
                )
            ).one()
            assert row[0] is None
            # Denormalised on purpose: once the topic is gone the slug is the
            # only thing that says which theme spent the credits.
            assert row[1] == "doomed"
            assert row[2] == 2
    finally:
        await engine.dispose()


async def _assert_fx_backfilled(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT estimated_notional, estimated_notional_account_currency, "
                        "       fx_required, fx_rate FROM trade_proposals WHERE id = :id"
                    ),
                    {"id": PROPOSAL},
                )
            ).one()
            # Exact, not approximate: `currency_alignment` blocked any mismatch,
            # so the two numbers were necessarily equal.
            assert row[1] == row[0]
            # False, not NULL. "No conversion was needed" and "unknown" are
            # different facts, and only one is true of a Phase 8 row.
            assert row[2] is False
            assert row[3] is None
    finally:
        await engine.dispose()


async def _assert_fx_constraints_bite(url: str) -> None:
    """Four constraints, each rejecting one way a record could mislead."""
    engine = create_async_engine(url)
    try:
        # Half a record is worse than none, because it looks auditable.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="fx_provenance_complete"):
                await conn.execute(
                    text(
                        "UPDATE trade_proposals SET fx_required = true, fx_rate = 1.35 "
                        "WHERE id = :id"
                    ),
                    {"id": PROPOSAL},
                )

        # `fx_required = false` with a rate attached is exactly the record a
        # silently-applied 1.0 would leave behind.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="fx_rate_requires_fx_required"):
                await conn.execute(
                    text(
                        "UPDATE trade_proposals SET fx_rate = 1.0, "
                        " fx_base_currency = 'GBP', fx_quote_currency = 'USD', "
                        " fx_provider = 'x', fx_provider_timestamp = now() WHERE id = :id"
                    ),
                    {"id": PROPOSAL},
                )

        # And the converse: a cross-currency size nobody can re-derive.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="fx_required_requires_rate"):
                await conn.execute(
                    text("UPDATE trade_proposals SET fx_required = true WHERE id = :id"),
                    {"id": PROPOSAL},
                )

        # Zero and negative are parsing failures, not market conditions.
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="fx_rate_positive"):
                await conn.execute(
                    text(
                        "UPDATE trade_proposals SET fx_required = true, fx_rate = 0, "
                        " fx_base_currency = 'GBP', fx_quote_currency = 'USD', "
                        " fx_provider = 'x', fx_provider_timestamp = now() WHERE id = :id"
                    ),
                    {"id": PROPOSAL},
                )

        # The shape the application actually writes satisfies all four together.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE trade_proposals SET "
                    " fx_required = true, fx_rate = 1.3521, fx_base_currency = 'GBP', "
                    " fx_quote_currency = 'USD', fx_direction = 'DIRECT', "
                    " fx_provider = 'frankfurter', fx_rate_grade = 'REFERENCE', "
                    " fx_rate_type = 'central_bank_reference', "
                    " fx_provider_timestamp = now(), fx_received_at = now(), "
                    " fx_age_seconds = 43200.000, "
                    " estimated_notional_account_currency = 443.86 WHERE id = :id"
                ),
                {"id": PROPOSAL},
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT fx_rate, fx_direction, fx_rate_grade, "
                        "       estimated_notional_account_currency "
                        "FROM trade_proposals WHERE id = :id"
                    ),
                    {"id": PROPOSAL},
                )
            ).one()
            assert row[1] == "DIRECT"
            assert row[2] == "REFERENCE"
            assert row[3] is not None
    finally:
        await engine.dispose()


async def _assert_downgraded(url: str) -> None:
    """Nothing left behind, and nothing lost.

    A downgrade that leaves an enum type behind makes the *next* upgrade fail
    with a duplicate-object error, which is the worst time to discover it.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            tables = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema='public'"
                        )
                    )
                ).all()
            }
            assert "firecrawl_calls" not in tables

            types = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT typname FROM pg_type WHERE typname IN "
                            "('firecrawl_call_kind', 'firecrawl_call_outcome')"
                        )
                    )
                ).all()
            }
            assert types == set()

            columns = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='discovery_queries'"
                        )
                    )
                ).all()
            }
            assert "next_eligible_at" not in columns
            assert "searches_performed" not in columns

            proposal_columns = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='trade_proposals'"
                        )
                    )
                ).all()
            }
            assert not any(name.startswith("fx_") for name in proposal_columns)

            constraints = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'trade_proposals'::regclass"
                        )
                    )
                ).all()
            }
            # No double-prefixed leftover from a mis-named drop (Phase 6 bug 13,
            # Phase 8 bug 18).
            assert not any(name.startswith("ck_trade_proposals_ck_") for name in constraints)

            # And the rows themselves survive.
            # Table names are literals in this tuple, never caller input, so
            # the interpolation is safe; the ids are still bound parameters.
            surviving = {
                "discovery_queries": QUERY,
                "sources": SCRAPED_SOURCE,
                "trade_proposals": PROPOSAL,
            }
            for table, key in surviving.items():
                count = (
                    await conn.execute(
                        text(
                            f"SELECT count(*) FROM {table} WHERE id = :id"  # noqa: S608
                        ),
                        {"id": key},
                    )
                ).scalar_one()
                assert count == 1, f"{table} lost its row on downgrade"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def test_the_migrations_survive_populated_tables(migrated_database: str) -> None:
    """The whole round trip, on a database that already has rows in it."""
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        asyncio.run(_seed_legacy(migrated_database))

        command.upgrade(config, FIRECRAWL_LEDGER)
        asyncio.run(_assert_cadence_backfilled(migrated_database))
        asyncio.run(_assert_content_marker_backfilled(migrated_database))
        asyncio.run(_assert_ledger_shape(migrated_database))

        command.upgrade(config, FX_PROVENANCE)
        asyncio.run(_assert_fx_backfilled(migrated_database))
        asyncio.run(_assert_fx_constraints_bite(migrated_database))

        command.downgrade(config, PREVIOUS)
        asyncio.run(_assert_downgraded(migrated_database))
    finally:
        command.upgrade(config, "head")


def test_the_phase_9_migrations_round_trip_twice(migrated_database: str) -> None:
    """base → head → base → head.

    Twice, because a downgrade that leaves an object behind fails on the
    *second* upgrade rather than the first -- which is how such a bug reaches
    production.
    """
    config = _config(migrated_database)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_no_phase_9_migration_imports_application_code() -> None:
    """A migration that imports mutable configuration does something different
    depending on when it is run, which is the one thing a migration must never
    do.

    The 720-minute floor in the Firecrawl migration is a frozen literal for
    exactly this reason: importing
    ``Settings.firecrawl_min_topic_interval_minutes`` would make the backfill
    depend on the operator's current ``.env``.
    """
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    phase_9 = sorted(versions.glob("20260905_2[23]*.py"))
    assert len(phase_9) == 2, "expected exactly the two Phase 9 migrations"
    for path in phase_9:
        source = path.read_text()
        assert "from stockbrain" not in source, path.name
        assert "import stockbrain" not in source, path.name

    firecrawl = (versions / "20260905_2200_firecrawl_budget.py").read_text()
    assert "_MIN_TOPIC_INTERVAL_MINUTES = 720" in firecrawl
