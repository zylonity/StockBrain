"""The provider-split migration against populated tables.

The rule this repository follows: *migrations must survive populated tables.*
Add nullable → backfill inside the migration → constrain, with every literal
frozen at the revision and no import of mutable application logic.

This one runs against a database that already holds the Phase 2 incident's
evidence, so four things must be true afterwards and none of them is automatic:

1. **The ledger keeps its rows and its meaning.**  ``firecrawl_calls`` becomes
   ``provider_calls`` by rename, every value intact, backfilled
   ``provider = 'firecrawl'`` -- which is what those rows were.
2. **Existing queries stay routine.**  Defaulting them to ``SEMANTIC`` would
   move keyword searches onto a provider that costs ten times as much.
3. **Provenance is not rewritten.**  A source discovered by Firecrawl search
   still says Firecrawl. Claiming Brave found it would be falsifying the record
   to make the new architecture look tidier.
4. **Retired job types do not become a retry storm.**  Terminal rows are
   history and are untouched; only a row that could still be *claimed* is
   cancelled, because after this revision nothing can run it.
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

FX_PROVENANCE = "b47e0c81f5a2"
WEB_DISCOVERY = "c3f28a1d6b45"

TOPIC = "88888888-1111-2222-3333-000000000001"
QUERY = "88888888-1111-2222-3333-000000000002"
FIRECRAWL_SOURCE = "88888888-1111-2222-3333-000000000003"
ALPACA_SOURCE = "88888888-1111-2222-3333-000000000004"
LEDGER_ROW = "88888888-1111-2222-3333-000000000005"
DEAD_JOB = "88888888-1111-2222-3333-000000000006"
PENDING_JOB = "88888888-1111-2222-3333-000000000007"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _seed_phase_9(url: str) -> None:
    """The world as it stands at ``b47e0c81f5a2``, incident evidence included."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO discovery_topics "
                    "(id, slug, name, enabled, interval_minutes, result_limit, freshness, "
                    " include_domains, exclude_domains, created_at, updated_at) "
                    "VALUES (:id, 'legacy_ai', 'Legacy AI', true, 720, 5, 'qdr:w', "
                    " '[]'::jsonb, '[]'::jsonb, now(), now())"
                ),
                {"id": TOPIC},
            )
            # Real counters: 470 credits on one query is the shape the live
            # database actually carries, and it is the only record of what the
            # incident cost.
            await conn.execute(
                text(
                    "INSERT INTO discovery_queries "
                    "(id, topic_id, query, enabled, last_run_at, next_eligible_at, "
                    " consecutive_failures, results_seen, credits_used, searches_performed, "
                    " created_at, updated_at) "
                    "VALUES (:id, :topic, '\"AI data center\" investment', true, "
                    " now() - interval '2 hours', now() + interval '10 hours', "
                    " 0, 42, 470, 29, now(), now())"
                ),
                {"id": QUERY, "topic": TOPIC},
            )

            insert_source = (
                "INSERT INTO sources "
                "(id, provider, canonical_url, original_url, headline, content_hash, "
                " received_at, raw_content, content_fetched_at, metadata, created_at, updated_at) "
                "VALUES (:id, :provider, :url, :url, 'A headline', :hash, now(), :body, "
                "        :fetched, '{}'::jsonb, now(), now())"
            )
            await conn.execute(
                text(insert_source),
                {
                    "id": FIRECRAWL_SOURCE,
                    "provider": "FIRECRAWL",
                    "url": "https://reuters.com/legacy-firecrawl",
                    "hash": "d" * 64,
                    "body": "# A full scraped article body",
                    "fetched": dt.datetime(2026, 9, 5, 5, 0, tzinfo=dt.UTC),
                },
            )
            await conn.execute(
                text(insert_source),
                {
                    "id": ALPACA_SOURCE,
                    "provider": "ALPACA",
                    "url": "https://alpaca.example/legacy",
                    "hash": "e" * 64,
                    "body": "An article that arrived with its body.",
                    "fetched": None,
                },
            )

            # One 402 from the incident: FAILED, charged, never reconciled.
            await conn.execute(
                text(
                    "INSERT INTO firecrawl_calls "
                    "(id, kind, outcome, reserved_at, query_id, topic_slug, requested_limit, "
                    " requested_sources, scrape_requested, credits_reserved, credits_charged, "
                    " pages_scraped, http_status, error_category) "
                    "VALUES (:id, 'SEARCH', 'FAILED', now() - interval '3 hours', :query, "
                    " 'legacy_ai', 5, '[\"web\", \"news\"]'::jsonb, false, 24, 24, 0, 402, "
                    " 'ProviderEntitlementError')"
                ),
                {"id": LEDGER_ROW, "query": QUERY},
            )

            insert_job = (
                "INSERT INTO jobs "
                "(id, job_type, payload, status, priority, run_after, attempts, max_attempts, "
                " created_at, updated_at) "
                "VALUES (:id, :type, '{}'::jsonb, :status, 60, now(), :attempts, 1, now(), now())"
            )
            # One of the 250 dead jobs from the 402 storm.
            await conn.execute(
                text(insert_job),
                {
                    "id": DEAD_JOB,
                    "type": "FIRECRAWL_TOPIC_SEARCH",
                    "status": "FAILED",
                    "attempts": 1,
                },
            )
            # And one that could still be claimed.
            await conn.execute(
                text(insert_job),
                {
                    "id": PENDING_JOB,
                    "type": "FIRECRAWL_ENRICH",
                    "status": "PENDING",
                    "attempts": 0,
                },
            )
    finally:
        await engine.dispose()


async def _assert_ledger_renamed_intact(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT provider, kind::text, outcome::text, units_reserved, "
                        "       units_charged, http_status, error_category, topic_slug, "
                        "       requested_sources, cost_usd_charged "
                        "FROM provider_calls WHERE id = :id"
                    ),
                    {"id": LEDGER_ROW},
                )
            ).one()
            # Every value survived the rename.
            assert row.provider == "firecrawl"
            assert row.kind == "SEARCH"
            assert row.outcome == "FAILED"
            assert row.units_reserved == 24
            assert row.units_charged == 24
            assert row.http_status == 402
            assert row.error_category == "ProviderEntitlementError"
            assert row.topic_slug == "legacy_ai"
            assert row.requested_sources == ["web", "news"]
            # Deliberately NULL: Firecrawl bills credits against a monthly
            # allowance rather than dollars per call, and a made-up per-call
            # dollar figure in the one table that exists to be believed would be
            # worse than none.
            assert row.cost_usd_charged is None

            # The old table name is gone, so nothing can write to it by mistake.
            assert (
                await conn.execute(text("SELECT to_regclass('firecrawl_calls')"))
            ).scalar_one() is None

            constraints = set(
                (
                    await conn.execute(
                        text(
                            "SELECT c.conname FROM pg_constraint c "
                            "JOIN pg_class t ON t.oid = c.conrelid "
                            "WHERE t.relname = 'provider_calls' AND c.contype = 'c'"
                        )
                    )
                ).scalars()
            )
            # Bare names, rendered once by the convention. Bugs 13, 18 and 26
            # are all one mistake: a rendered `ck_...` passed to a constraint
            # helper produces a name no later migration can drop.
            assert constraints == {
                "ck_provider_calls_units_reserved_non_negative",
                "ck_provider_calls_units_charged_non_negative",
                "ck_provider_calls_pages_scraped_non_negative",
            }
            assert not any(name.startswith("ck_provider_calls_ck_") for name in constraints)
    finally:
        await engine.dispose()


async def _assert_queries_are_routine(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT search_kind::text, provider, interval_minutes, result_limit, "
                        "       priority, credits_used, searches_performed, next_eligible_at "
                        "FROM discovery_queries WHERE id = :id"
                    ),
                    {"id": QUERY},
                )
            ).one()
            # Keyword searches stay keyword searches. Defaulting them to
            # SEMANTIC would move them onto a provider that costs ten times as
            # much, on the first sweep after deployment.
            assert row.search_kind == "ROUTINE"
            # No pin, no per-query overrides: the deployment's configured
            # default answers them, exactly as before.
            assert row.provider is None
            assert row.interval_minutes is None
            assert row.result_limit is None
            assert row.priority == 100
            # The incident's counters survive untouched.
            assert row.credits_used == 470
            assert row.searches_performed == 29
            # And the cooldown is not reset -- a migration that cleared it would
            # re-run every query the moment it shipped.
            assert row.next_eligible_at is not None
    finally:
        await engine.dispose()


async def _assert_freshness_translated(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            days = (
                await conn.execute(
                    text("SELECT freshness_days FROM discovery_topics WHERE id = :id"),
                    {"id": TOPIC},
                )
            ).scalar_one()
            # ``qdr:w`` is a week. A topic configured for the past week stays
            # configured for the past week rather than silently becoming the
            # column default.
            assert days == 7
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'discovery_topics' AND column_name = 'freshness'"
                    )
                )
            ).scalar_one() == 0
    finally:
        await engine.dispose()


async def _assert_provenance_preserved(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = {
                str(row.id): row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, provider::text AS provider, discovered_by, "
                            "       extraction_method FROM sources"
                        )
                    )
                ).all()
            }
            firecrawl = rows[FIRECRAWL_SOURCE]
            # **Not rewritten.** Firecrawl found it; claiming Brave did would
            # falsify the record to make the new architecture look tidier.
            assert firecrawl.provider == "FIRECRAWL"
            assert firecrawl.discovered_by == ["FIRECRAWL"]
            # It has a body and it was paid for, which is exactly what the row
            # already proves -- this asserts nothing new about it.
            assert firecrawl.extraction_method == "FIRECRAWL"

            alpaca = rows[ALPACA_SOURCE]
            assert alpaca.discovered_by == ["ALPACA"]
            # Alpaca delivers the body with the item, so it was never fetched.
            assert alpaca.extraction_method == "PROVIDER"

            # The new provider labels exist but nothing has been relabelled.
            providers = set(
                (await conn.execute(text("SELECT DISTINCT provider::text FROM sources"))).scalars()
            )
            assert providers == {"FIRECRAWL", "ALPACA"}
            enum_values = set(
                (
                    await conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                            "ON t.oid = e.enumtypid WHERE t.typname = 'source_provider'"
                        )
                    )
                ).scalars()
            )
            assert {"BRAVE", "EXA"} <= enum_values
    finally:
        await engine.dispose()


async def _assert_jobs_retired_safely(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            dead = (
                await conn.execute(
                    text("SELECT status::text, last_error FROM jobs WHERE id = :id"),
                    {"id": DEAD_JOB},
                )
            ).one()
            # Terminal rows are history and are left exactly as they are --
            # including the 250 from the 402 storm.
            assert dead.status == "FAILED"
            assert dead.last_error is None

            pending = (
                await conn.execute(
                    text("SELECT status::text, last_error FROM jobs WHERE id = :id"),
                    {"id": PENDING_JOB},
                )
            ).one()
            # A claimable row for a job type with no handler would be claimed,
            # fail, and be retried until it exhausted its attempts.
            assert pending.status == "CANCELLED"
            assert "retired" in pending.last_error
    finally:
        await engine.dispose()


async def _assert_downgraded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT credits_reserved, credits_charged, kind::text "
                        "FROM firecrawl_calls WHERE id = :id"
                    ),
                    {"id": LEDGER_ROW},
                )
            ).one()
            # The rename is reversible and loses nothing.
            assert row.credits_reserved == 24
            assert row.credits_charged == 24
            assert row.kind == "SEARCH"

            freshness = (
                await conn.execute(
                    text("SELECT freshness FROM discovery_topics WHERE id = :id"),
                    {"id": TOPIC},
                )
            ).scalar_one()
            assert freshness == "qdr:w"

            for table, key in (("sources", FIRECRAWL_SOURCE), ("discovery_queries", QUERY)):
                count = (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE id = :id"),  # noqa: S608
                        {"id": key},
                    )
                ).scalar_one()
                assert count == 1, f"{table} lost its row on downgrade"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def test_the_provider_split_survives_a_populated_database(migrated_database: str) -> None:
    """The whole round trip, on a database that already has the incident in it."""
    config = _config(migrated_database)
    try:
        command.downgrade(config, FX_PROVENANCE)
        asyncio.run(_seed_phase_9(migrated_database))

        command.upgrade(config, WEB_DISCOVERY)
        asyncio.run(_assert_ledger_renamed_intact(migrated_database))
        asyncio.run(_assert_queries_are_routine(migrated_database))
        asyncio.run(_assert_freshness_translated(migrated_database))
        asyncio.run(_assert_provenance_preserved(migrated_database))
        asyncio.run(_assert_jobs_retired_safely(migrated_database))

        command.downgrade(config, FX_PROVENANCE)
        asyncio.run(_assert_downgraded(migrated_database))
    finally:
        command.upgrade(config, "head")


def test_the_migration_round_trips_twice(migrated_database: str) -> None:
    """head → previous → head → previous → head.

    Twice, because a downgrade that leaves an object behind fails on the
    *second* upgrade rather than the first -- which is how such a bug reaches
    production.
    """
    config = _config(migrated_database)
    try:
        for _ in range(2):
            command.downgrade(config, FX_PROVENANCE)
            command.upgrade(config, WEB_DISCOVERY)
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_the_migration_imports_no_application_code() -> None:
    """A migration that imports ``stockbrain`` re-runs today's logic against
    yesterday's schema.  Every literal it needs is frozen in the file."""
    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260905_2400_web_discovery_providers.py"
    ).read_text()
    imports = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]
    assert not any("stockbrain" in line for line in imports), imports
