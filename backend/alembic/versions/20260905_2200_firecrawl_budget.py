"""Firecrawl call ledger, durable search cadence, and fetched-content marker.

Revision ID: 9a1c4d2b7e31
Revises: 5f2a7c93e410

Phase 9's cost-control migration.  What it adds and why:

1. **``firecrawl_calls``** -- the durable Firecrawl budget.  Before this, credit
   accounting lived in an integer on a client object and a Prometheus counter,
   neither of which survives a restart and neither of which can refuse a call.
   A row is written and committed *before* each paid request, so the number the
   caps are compared against can only ever over-state the spend.

2. **``discovery_queries.next_eligible_at``** -- a cooldown that is a column
   rather than a subtraction.  ``last_run_at + interval`` computed at read time
   is fine until the interval changes, until a failure needs a different
   cooldown from a success, or until a restart re-runs everything.  All three
   happened.

   Backfilled from ``last_run_at`` plus the **new** floor, deliberately:
   existing rows must not become immediately eligible the moment this ships,
   which is the reading a plain ``NULL`` would get.  The floor is a frozen
   literal here (720 minutes) rather than an import of
   ``Settings.firecrawl_min_topic_interval_minutes`` -- a migration that imports
   mutable application configuration does something different depending on when
   it is run, which is the one thing a migration must never do.

3. **``sources.content_fetched_at``** plus a partial index -- the two-stage
   consumption model needs "do we already have the body" to be a fact on the
   row.  Backfilled for the rows that already carry scraped markdown, from the
   ``has_scraped_markdown`` flag Phase 2 wrote into ``metadata``, so the 117
   already-scraped rows in a populated database are not paid for twice.

No application code is imported.  Every literal is frozen at this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a1c4d2b7e31"
down_revision: str | None = "5f2a7c93e410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen copy of ``FIRECRAWL_MIN_TOPIC_INTERVAL_MINUTES``'s default at this
#: revision, used only to backfill ``next_eligible_at``.  A literal on purpose:
#: see the module docstring.
_MIN_TOPIC_INTERVAL_MINUTES = 720


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. The ledger
    # ------------------------------------------------------------------
    # Created explicitly, then referenced with ``create_type=False``. Letting
    # ``create_table`` emit the CREATE TYPE as a side effect works once and then
    # collides with this explicit create on the next run; being explicit about
    # both halves is what makes the downgrade able to drop them again.
    sa.Enum("SEARCH", "SCRAPE", name="firecrawl_call_kind").create(op.get_bind(), checkfirst=True)
    sa.Enum("RESERVED", "SUCCEEDED", "FAILED", name="firecrawl_call_outcome").create(
        op.get_bind(), checkfirst=True
    )
    firecrawl_call_kind = postgresql.ENUM(
        "SEARCH", "SCRAPE", name="firecrawl_call_kind", create_type=False
    )
    firecrawl_call_outcome = postgresql.ENUM(
        "RESERVED", "SUCCEEDED", "FAILED", name="firecrawl_call_outcome", create_type=False
    )

    op.create_table(
        "firecrawl_calls",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", firecrawl_call_kind, nullable=False),
        sa.Column(
            "outcome",
            firecrawl_call_outcome,
            nullable=False,
            server_default="RESERVED",
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("query_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("topic_slug", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("target_url", sa.Text(), nullable=True),
        sa.Column("requested_limit", sa.Integer(), nullable=True),
        sa.Column(
            "requested_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("scrape_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("credits_reserved", sa.Integer(), nullable=False),
        sa.Column("credits_reported", sa.Integer(), nullable=True),
        sa.Column("credits_charged", sa.Integer(), nullable=False),
        sa.Column("results_returned", sa.Integer(), nullable=True),
        sa.Column("pages_scraped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_firecrawl_calls"),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["discovery_queries.id"],
            name="fk_firecrawl_calls_query_id_discovery_queries",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_firecrawl_calls_source_id_sources",
            ondelete="SET NULL",
        ),
        # **Bare** names. The metadata naming convention
        # (`ck_%(table_name)s_%(constraint_name)s`) is applied here too, so an
        # already-rendered `ck_firecrawl_calls_...` gets the prefix twice and is
        # then truncated to PostgreSQL's 63 characters with a hash suffix --
        # producing `ck_firecrawl_calls_ck_firecrawl_calls_credits_charged_n_4fc8`,
        # a name that also disagrees with the model's. This is Phase 6's bug 13
        # and Phase 8's bug 18 for the third time; caught here by
        # `test_migration_phase9.py` asserting on the constraint name.
        sa.CheckConstraint("credits_reserved >= 0", name="credits_reserved_non_negative"),
        sa.CheckConstraint("credits_charged >= 0", name="credits_charged_non_negative"),
        sa.CheckConstraint("pages_scraped >= 0", name="pages_scraped_non_negative"),
    )
    op.create_index("ix_firecrawl_calls_reserved_at", "firecrawl_calls", ["reserved_at"])
    op.create_index(
        "ix_firecrawl_calls_kind_reserved_at", "firecrawl_calls", ["kind", "reserved_at"]
    )
    op.create_index("ix_firecrawl_calls_query_id", "firecrawl_calls", ["query_id"])

    # ------------------------------------------------------------------
    # 2. Durable search cadence
    # ------------------------------------------------------------------
    op.add_column(
        "discovery_queries",
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discovery_queries",
        sa.Column("searches_performed", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # A query that has run before is not eligible again until a full *new*
    # interval has passed. Leaving these NULL would make every existing query
    # due at once on the first sweep after deployment -- which is a smaller
    # version of the incident this migration exists because of.
    op.execute(
        sa.text(
            "UPDATE discovery_queries "
            "SET next_eligible_at = last_run_at + make_interval(mins => :minutes) "
            "WHERE last_run_at IS NOT NULL"
        ).bindparams(minutes=_MIN_TOPIC_INTERVAL_MINUTES)
    )

    # ------------------------------------------------------------------
    # 3. Fetched-content marker
    # ------------------------------------------------------------------
    op.add_column(
        "sources",
        sa.Column("content_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Phase 2 recorded whether a search result arrived with scraped markdown in
    # the source's metadata. Those rows already have their body, and paying to
    # fetch it again would be the migration itself costing money.
    op.execute(
        sa.text(
            "UPDATE sources SET content_fetched_at = received_at "
            "WHERE provider = 'FIRECRAWL' "
            "AND metadata ->> 'has_scraped_markdown' = 'true'"
        )
    )
    op.create_index(
        "ix_sources_unfetched_content",
        "sources",
        ["provider", "received_at"],
        postgresql_where=sa.text("content_fetched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sources_unfetched_content", table_name="sources")
    op.drop_column("sources", "content_fetched_at")

    op.drop_column("discovery_queries", "searches_performed")
    op.drop_column("discovery_queries", "next_eligible_at")

    op.drop_index("ix_firecrawl_calls_query_id", table_name="firecrawl_calls")
    op.drop_index("ix_firecrawl_calls_kind_reserved_at", table_name="firecrawl_calls")
    op.drop_index("ix_firecrawl_calls_reserved_at", table_name="firecrawl_calls")
    op.drop_table("firecrawl_calls")
    # Dropped after the table, and by bare name: `op.drop_constraint` and the
    # Enum helpers both apply the naming convention, and a pre-rendered name
    # gets the prefix twice (Phase 6 bug 13, Phase 8 bug 18).
    sa.Enum(name="firecrawl_call_outcome").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="firecrawl_call_kind").drop(op.get_bind(), checkfirst=True)
