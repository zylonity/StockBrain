"""Persisted historical news-and-candle cache.

Revision ID: e93b7c14a6d2
Revises: d47f2a9c81e5

Adds the four tables behind :mod:`stockbrain.db.models.news_history` and
:mod:`stockbrain.intelligence.news_history`: cached historical articles grouped
onto the existing event graph, per-symbol OHLCV candles, per-scope coverage
watermarks, and one row per explicit bounded backfill run.

``source_provider`` already exists (the ``sources`` table owns it), so it is
referenced with ``create_type=False`` and never recreated here.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e93b7c14a6d2"
down_revision: str | None = "d47f2a9c81e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_history_articles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            postgresql.ENUM(
                "ALPACA",
                "BRAVE",
                "EXA",
                "FIRECRAWL",
                "SEC",
                "MANUAL",
                "INVESTEGATE",
                "EQS",
                "CNMV",
                "GLOBENEWSWIRE",
                "ACTUSNEWS",
                name="source_provider",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("provider_item_id", sa.Text(), nullable=True),
        sa.Column("feed", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column(
            "symbols",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_history_articles")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="SET NULL",
            name=op.f("fk_news_history_articles_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="SET NULL",
            name=op.f("fk_news_history_articles_event_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            ondelete="SET NULL",
            name=op.f("fk_news_history_articles_source_id_sources"),
        ),
    )
    op.create_index(
        "uq_news_history_articles_provider_item",
        "news_history_articles",
        ["provider", "provider_item_id"],
        unique=True,
        postgresql_where=sa.text("provider_item_id IS NOT NULL"),
    )
    op.create_index(
        "ix_news_history_articles_observed_at", "news_history_articles", ["observed_at"]
    )
    op.create_index(
        "ix_news_history_articles_company_event_observed",
        "news_history_articles",
        ["company_id", "event_type", "observed_at"],
    )
    op.create_index(
        "ix_news_history_articles_event_id", "news_history_articles", ["event_id"]
    )
    op.create_index(
        "ix_news_history_articles_content_hash", "news_history_articles", ["content_hash"]
    )

    op.create_table(
        "news_history_candles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), server_default="1Min", nullable=False),
        sa.Column("provider", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("feed", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("bar_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("open", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("high", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("low", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("close", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("volume", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_history_candles")),
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "bar_start",
            "feed",
            name="uq_news_history_candles_identity",
        ),
    )
    op.create_index(
        "ix_news_history_candles_symbol_timeframe_start",
        "news_history_candles",
        ["symbol", "timeframe", "bar_start"],
    )
    op.create_index(
        "ix_news_history_candles_observed_at", "news_history_candles", ["observed_at"]
    )

    op.create_table(
        "news_history_coverage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("covered_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("article_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bar_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "is_partial", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("detail", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_history_coverage")),
        sa.UniqueConstraint(
            "dataset", "scope_key", name="uq_news_history_coverage_scope"
        ),
    )
    op.create_index(
        "ix_news_history_coverage_dataset", "news_history_coverage", ["dataset"]
    )

    op.create_table(
        "news_history_backfill_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="RUNNING", nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("articles_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bars_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("requests_made", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bounded", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_history_backfill_runs")),
    )
    op.create_index(
        "ix_news_history_backfill_runs_dataset_started",
        "news_history_backfill_runs",
        ["dataset", "started_at"],
    )
    op.create_index(
        "ix_news_history_backfill_runs_status", "news_history_backfill_runs", ["status"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_news_history_backfill_runs_status", table_name="news_history_backfill_runs"
    )
    op.drop_index(
        "ix_news_history_backfill_runs_dataset_started",
        table_name="news_history_backfill_runs",
    )
    op.drop_table("news_history_backfill_runs")

    op.drop_index("ix_news_history_coverage_dataset", table_name="news_history_coverage")
    op.drop_table("news_history_coverage")

    op.drop_index("ix_news_history_candles_observed_at", table_name="news_history_candles")
    op.drop_index(
        "ix_news_history_candles_symbol_timeframe_start", table_name="news_history_candles"
    )
    op.drop_table("news_history_candles")

    op.drop_index("ix_news_history_articles_content_hash", table_name="news_history_articles")
    op.drop_index("ix_news_history_articles_event_id", table_name="news_history_articles")
    op.drop_index(
        "ix_news_history_articles_company_event_observed",
        table_name="news_history_articles",
    )
    op.drop_index("ix_news_history_articles_observed_at", table_name="news_history_articles")
    op.drop_index(
        "uq_news_history_articles_provider_item", table_name="news_history_articles"
    )
    op.drop_table("news_history_articles")
