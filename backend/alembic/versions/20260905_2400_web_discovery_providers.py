"""Provider-agnostic web discovery: Brave, Exa, and Firecrawl as fallback only.

Revision ID: c3f28a1d6b45
Revises: b47e0c81f5a2

Firecrawl stops being the primary discovery provider.  Brave answers routine
thematic search, Exa answers semantic second-order search, and Firecrawl keeps
one job: a paid page fetch when free local extraction cannot read a page.

Five changes, and each one is shaped by the fact that this runs against a
**populated** database that already carries the Phase 2 incident's evidence.

**1. ``firecrawl_calls`` becomes ``provider_calls``.**  Renamed rather than
replaced, because those rows are the only record of what the incident cost and
because a second ledger would mean two implementations of the one thing that
can refuse a paid call.  Existing rows are backfilled ``provider = 'firecrawl'``
-- which is what they were.  The ``credits_*`` columns become ``units_*``: for
Firecrawl a unit is a credit, for Brave and Exa it is a request, and calling a
Brave request a "credit" would have made the column's meaning depend on which
row you were looking at.  ``RENAME`` preserves every value.

**2. Discovery queries gain a taxonomy.**  ``search_kind`` (ROUTINE / SEMANTIC),
an optional provider pin, and per-query cadence, limit and priority.  Every
existing row is ``ROUTINE``: they are keyword searches, and defaulting them to
SEMANTIC would move them onto a provider that costs ten times as much.

**3. ``discovery_topics.freshness`` becomes ``freshness_days``.**  The old
column held Firecrawl's ``tbs`` token (``qdr:d``), which Brave and Exa cannot
read.  Backfilled by translating each token to its number of days, so a topic
configured for "past week" stays configured for the past week.

**4. Sources record how they were read and who found them.**
``extraction_method`` and ``discovered_by`` -- the second so that Exa surfacing
a page Brave already ingested is recorded as corroboration rather than
discarded.  Existing rows are backfilled with their own provider, which is
true: one provider found them.

**5. Old job types are retired safely.**  ``FIRECRAWL_TOPIC_SEARCH`` and
``FIRECRAWL_ENRICH`` have no handler after this revision.  Terminal rows
(SUCCEEDED / FAILED / CANCELLED) are **left exactly as they are** -- including
the 250 dead jobs from the 402 storm, which are history.  Only rows that could
still be *claimed* are cancelled, because an unregistered job type would be
claimed, fail, and be retried until it exhausted its attempts.

Nothing is deleted and no provenance is rewritten.  A row discovered by
Firecrawl search still says so.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "c3f28a1d6b45"
down_revision: str | None = "b47e0c81f5a2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


#: Firecrawl's ``tbs`` vocabulary, translated to days.  ``qdr:h`` maps to 1
#: rather than to a fraction: the column is whole days, and rounding an hour
#: *up* to a day widens the window, which returns a superset the classifier
#: discards for free.  Rounding down would silently drop results.
_FRESHNESS_DAYS = {
    "qdr:h": 1,
    "qdr:d": 1,
    "qdr:w": 7,
    "qdr:m": 31,
    "qdr:y": 365,
}


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. The ledger: firecrawl_calls -> provider_calls
    # ------------------------------------------------------------------
    # ALTER TYPE ... RENAME is metadata-only and keeps every value and every
    # dependent column. Recreating the types would have required rewriting the
    # column, which on a populated ledger is exactly the risk to avoid.
    op.execute("ALTER TYPE firecrawl_call_kind RENAME TO provider_call_kind")
    op.execute("ALTER TYPE firecrawl_call_outcome RENAME TO provider_call_outcome")
    op.rename_table("firecrawl_calls", "provider_calls")

    op.alter_column("provider_calls", "credits_reserved", new_column_name="units_reserved")
    op.alter_column("provider_calls", "credits_reported", new_column_name="units_reported")
    op.alter_column("provider_calls", "credits_charged", new_column_name="units_charged")

    op.add_column("provider_calls", sa.Column("provider", sa.Text(), nullable=True))
    # Backfilled before the NOT NULL, because every row that exists was a
    # Firecrawl call -- there was no other metered provider.
    op.execute("UPDATE provider_calls SET provider = 'firecrawl' WHERE provider IS NULL")
    op.alter_column("provider_calls", "provider", nullable=False)

    op.add_column(
        "provider_calls", sa.Column("cost_usd_reported", sa.Numeric(12, 6), nullable=True)
    )
    op.add_column("provider_calls", sa.Column("cost_usd_charged", sa.Numeric(12, 6), nullable=True))
    # Deliberately left NULL for the backfilled Firecrawl rows. Firecrawl bills
    # credits against a monthly allowance rather than dollars per call, and
    # inventing a per-call dollar figure for them would put a fabricated number
    # in the one table that exists to be believed.

    # Index and constraint names carry the old table's name after a rename, so
    # each is renamed explicitly.
    for old, new in (
        ("ix_firecrawl_calls_reserved_at", "ix_provider_calls_reserved_at"),
        ("ix_firecrawl_calls_kind_reserved_at", "ix_provider_calls_kind_reserved_at"),
        ("ix_firecrawl_calls_query_id", "ix_provider_calls_query_id"),
    ):
        op.execute(f'ALTER INDEX IF EXISTS "{old}" RENAME TO "{new}"')
    for old, new in (
        ("pk_firecrawl_calls", "pk_provider_calls"),
        (
            "fk_firecrawl_calls_query_id_discovery_queries",
            "fk_provider_calls_query_id_discovery_queries",
        ),
        ("fk_firecrawl_calls_source_id_sources", "fk_provider_calls_source_id_sources"),
    ):
        _rename_constraint(bind, "provider_calls", old, new)

    # The check constraints were named for the credit columns they guarded, and
    # they are dropped rather than renamed because the expression references the
    # renamed columns.
    #
    # Discovered from the catalogue rather than listed, because two name shapes
    # exist in the wild: the fixed Phase 9 migration produces
    # `ck_firecrawl_calls_credits_charged_non_negative`, and a database migrated
    # before that fix carries the double-prefixed
    # `ck_firecrawl_calls_ck_firecrawl_calls_credits_charged_n_4fc8` (bug 26).
    # A hard-coded list would strand one of the two.
    for name in _check_constraints(bind, "provider_calls"):
        op.execute(f'ALTER TABLE provider_calls DROP CONSTRAINT IF EXISTS "{name}"')
    # Bare names -- passing a rendered `ck_...` is bug 26, and this is the fourth
    # place that mistake has been available.
    op.create_check_constraint(
        "units_reserved_non_negative", "provider_calls", "units_reserved >= 0"
    )
    op.create_check_constraint("units_charged_non_negative", "provider_calls", "units_charged >= 0")
    op.create_check_constraint("pages_scraped_non_negative", "provider_calls", "pages_scraped >= 0")
    op.create_index(
        "ix_provider_calls_provider_reserved_at", "provider_calls", ["provider", "reserved_at"]
    )

    # ------------------------------------------------------------------
    # 2. Query taxonomy
    # ------------------------------------------------------------------
    web_discovery_kind = sa.Enum("ROUTINE", "SEMANTIC", name="web_discovery_kind")
    web_discovery_kind.create(bind, checkfirst=True)
    op.add_column(
        "discovery_queries",
        sa.Column(
            "search_kind",
            web_discovery_kind,
            nullable=False,
            server_default="ROUTINE",
        ),
    )
    op.add_column("discovery_queries", sa.Column("provider", sa.Text(), nullable=True))
    op.add_column("discovery_queries", sa.Column("interval_minutes", sa.Integer(), nullable=True))
    op.add_column("discovery_queries", sa.Column("result_limit", sa.Integer(), nullable=True))
    op.add_column(
        "discovery_queries",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
    )

    # ------------------------------------------------------------------
    # 3. Freshness: a provider token becomes a number of days
    # ------------------------------------------------------------------
    op.add_column(
        "discovery_topics",
        sa.Column("freshness_days", sa.Integer(), nullable=False, server_default="7"),
    )
    for token, days in _FRESHNESS_DAYS.items():
        op.execute(
            sa.text(
                "UPDATE discovery_topics SET freshness_days = :days WHERE freshness = :token"
            ).bindparams(days=days, token=token)
        )
    op.drop_column("discovery_topics", "freshness")

    # ------------------------------------------------------------------
    # 4. Source provenance and extraction method
    # ------------------------------------------------------------------
    op.add_column("sources", sa.Column("extraction_method", sa.Text(), nullable=True))
    op.add_column(
        "sources",
        sa.Column(
            "discovered_by",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # True for every existing row: one provider found it, and that provider is
    # already recorded. This is not a guess.
    op.execute(
        "UPDATE sources SET discovered_by = jsonb_build_array(provider::text) "
        "WHERE discovered_by = '[]'::jsonb"
    )
    # Alpaca and SEC deliver the body with the item; a search provider delivers
    # a snippet, and `content_fetched_at` records whether a body was ever
    # fetched for one. Both statements assert only what the row already proves.
    op.execute(
        "UPDATE sources SET extraction_method = 'PROVIDER' "
        "WHERE provider IN ('ALPACA', 'SEC', 'MANUAL')"
    )
    op.execute(
        "UPDATE sources SET extraction_method = 'FIRECRAWL' "
        "WHERE provider = 'FIRECRAWL' AND content_fetched_at IS NOT NULL"
    )

    # `SourceProvider` gains two members. IF NOT EXISTS so a database that has
    # somehow seen them already is not stranded; ADD VALUE cannot run inside a
    # transaction on older PostgreSQL, but is permitted from 12 onward and this
    # deployment is on 17.
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'BRAVE'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'EXA'")
    op.execute("ALTER TYPE provider_status ADD VALUE IF NOT EXISTS 'BUDGET_EXHAUSTED'")

    # ------------------------------------------------------------------
    # 5. Retire the old job types without erasing their history
    # ------------------------------------------------------------------
    # Terminal rows are left untouched -- the 250 dead jobs from the 402 storm
    # are evidence. Only a row that could still be claimed is cancelled, because
    # after this revision nothing can run it and the queue would retry it until
    # it exhausted its attempts.
    op.execute(
        "UPDATE jobs SET status = 'CANCELLED', "
        "last_error = 'job type retired by the multi-provider discovery migration', "
        "locked_by = NULL, locked_at = NULL, updated_at = now() "
        "WHERE job_type IN ('FIRECRAWL_TOPIC_SEARCH', 'FIRECRAWL_ENRICH') "
        "AND status IN ('PENDING', 'RUNNING')"
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute(
        "UPDATE jobs SET status = 'PENDING' "
        "WHERE job_type IN ('FIRECRAWL_TOPIC_SEARCH', 'FIRECRAWL_ENRICH') "
        "AND status = 'CANCELLED' "
        "AND last_error = 'job type retired by the multi-provider discovery migration'"
    )

    op.drop_column("sources", "discovered_by")
    op.drop_column("sources", "extraction_method")

    op.add_column(
        "discovery_topics",
        sa.Column("freshness", sa.Text(), nullable=False, server_default="qdr:d"),
    )
    op.execute("UPDATE discovery_topics SET freshness = 'qdr:d' WHERE freshness_days <= 1")
    op.execute(
        "UPDATE discovery_topics SET freshness = 'qdr:w' "
        "WHERE freshness_days > 1 AND freshness_days <= 7"
    )
    op.execute(
        "UPDATE discovery_topics SET freshness = 'qdr:m' "
        "WHERE freshness_days > 7 AND freshness_days <= 31"
    )
    op.execute("UPDATE discovery_topics SET freshness = 'qdr:y' WHERE freshness_days > 31")
    op.drop_column("discovery_topics", "freshness_days")

    op.drop_column("discovery_queries", "priority")
    op.drop_column("discovery_queries", "result_limit")
    op.drop_column("discovery_queries", "interval_minutes")
    op.drop_column("discovery_queries", "provider")
    op.drop_column("discovery_queries", "search_kind")
    sa.Enum(name="web_discovery_kind").drop(bind, checkfirst=True)

    op.drop_index("ix_provider_calls_provider_reserved_at", table_name="provider_calls")
    # Bare names, in the downgrade too. Passing a rendered `ck_...` here is the
    # other half of bug 26.
    op.drop_constraint("pages_scraped_non_negative", "provider_calls", type_="check")
    op.drop_constraint("units_charged_non_negative", "provider_calls", type_="check")
    op.drop_constraint("units_reserved_non_negative", "provider_calls", type_="check")

    op.drop_column("provider_calls", "cost_usd_charged")
    op.drop_column("provider_calls", "cost_usd_reported")
    op.drop_column("provider_calls", "provider")

    op.alter_column("provider_calls", "units_charged", new_column_name="credits_charged")
    op.alter_column("provider_calls", "units_reported", new_column_name="credits_reported")
    op.alter_column("provider_calls", "units_reserved", new_column_name="credits_reserved")

    for new, old in (
        ("ix_provider_calls_reserved_at", "ix_firecrawl_calls_reserved_at"),
        ("ix_provider_calls_kind_reserved_at", "ix_firecrawl_calls_kind_reserved_at"),
        ("ix_provider_calls_query_id", "ix_firecrawl_calls_query_id"),
    ):
        op.execute(f'ALTER INDEX IF EXISTS "{new}" RENAME TO "{old}"')
    for new, old in (
        ("pk_provider_calls", "pk_firecrawl_calls"),
        (
            "fk_provider_calls_query_id_discovery_queries",
            "fk_firecrawl_calls_query_id_discovery_queries",
        ),
        ("fk_provider_calls_source_id_sources", "fk_firecrawl_calls_source_id_sources"),
    ):
        _rename_constraint(bind, "provider_calls", new, old)

    op.create_check_constraint(
        "credits_reserved_non_negative", "provider_calls", "credits_reserved >= 0"
    )
    op.create_check_constraint(
        "credits_charged_non_negative", "provider_calls", "credits_charged >= 0"
    )
    op.create_check_constraint("pages_scraped_non_negative", "provider_calls", "pages_scraped >= 0")

    op.rename_table("provider_calls", "firecrawl_calls")
    op.execute("ALTER TYPE provider_call_kind RENAME TO firecrawl_call_kind")
    op.execute("ALTER TYPE provider_call_outcome RENAME TO firecrawl_call_outcome")

    # `source_provider` keeps BRAVE and EXA, and `provider_status` keeps
    # BUDGET_EXHAUSTED. PostgreSQL cannot remove an enum value, and recreating
    # the type would rewrite every source row -- for no benefit, since an unused
    # value is inert. Downgrading with Brave-discovered rows present would
    # otherwise be impossible, and a downgrade that destroys evidence is worse
    # than one that leaves a spare label.


def _rename_constraint(bind: sa.engine.Connection, table: str, old: str, new: str) -> None:
    """Rename a constraint only if it is actually there under the old name.

    Renaming a primary key's index already renames its constraint, so a blind
    ``RENAME CONSTRAINT`` afterwards would error on a name that no longer
    exists.  Checked rather than suppressed, because an unconditional
    ``IF EXISTS`` is not available for this statement.
    """
    present = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :table AND c.conname = :name"
        ).bindparams(table=table, name=old)
    ).scalar()
    if present:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def _check_constraints(bind: sa.engine.Connection, table: str) -> list[str]:
    """Every CHECK constraint on a table, by name."""
    rows = bind.execute(
        sa.text(
            "SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :table AND c.contype = 'c'"
        ).bindparams(table=table)
    ).scalars()
    return [str(name) for name in rows]
