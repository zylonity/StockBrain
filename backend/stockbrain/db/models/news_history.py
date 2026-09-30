"""Persisted historical cache for news articles and minute OHLCV candles.

This is deliberately separate from the live discovery path.  The live
``sources``/``events`` tables answer "what did we just see?" and are optimised
for ingestion.  This cache answers a different question, asked by research and
by outcome grading after the fact:

    "What was knowable about this company at time T, and where did it come
    from?"

Four tables carry that:

* :class:`NewsHistoryArticle` -- one backfilled article, stamped with
  ``observed_at`` (when the world could have known it) and ``ingested_at`` (when
  the cache learned it). Actual live receipt is unknown. Articles describing
  the same occurrence share an archived ``event_id`` for duplicate suppression.
  Historical imports create no live Source or EventSourceLink and cannot enter
  the classification sweep.

* :class:`NewsHistoryCandle` -- one per-symbol minute (or other timeframe) bar,
  with the feed it came from.  The identity is
  ``(symbol, timeframe, bar_start, feed)`` so a re-fetch is an idempotent
  upsert rather than a duplicate.

* :class:`NewsHistoryCoverage` -- the watermark and completeness record for one
  dataset scope (a symbol, or ``GLOBAL`` for the news firehose).  Partial
  coverage is first-class: ``is_partial`` is True whenever a bound stopped the
  backfill before the requested window was covered.

* :class:`NewsHistoryBackfillRun` -- one explicit, on-demand backfill attempt,
  with the caps it was given and the counters it consumed.  It exists so that
  "the cache stops when told to" is auditable rather than a promise.

Nothing in this module is imported by the per-research-run path.  A backfill is
started explicitly (operator action or a scheduled job); a research run only ever
reads what the cache already holds.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import Mapped, mapped_column

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import SourceProvider

__all__ = [
    "DATASET_CANDLES",
    "DATASET_NEWS",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_PARTIAL",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCEEDED",
    "NewsHistoryArticle",
    "NewsHistoryBackfillRun",
    "NewsHistoryCandle",
    "NewsHistoryCoverage",
]

#: Coverage/run dataset labels.  Plain strings rather than a native enum so a
#: later dataset (options, fundamentals) is a data change, not a type migration.
DATASET_NEWS = "NEWS"
DATASET_CANDLES = "CANDLES"

#: Lifecycle of one explicit backfill attempt.
RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_SUCCEEDED = "SUCCEEDED"
RUN_STATUS_PARTIAL = "PARTIAL"
RUN_STATUS_FAILED = "FAILED"


class NewsHistoryArticle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cached historical news article, grouped onto a canonical event.

    ``observed_at`` is the point-in-time boundary: a query "as of T" returns only
    rows whose ``observed_at <= T``.  It is derived from the provider timestamp
    (``published_at`` when present) because that is when the fact became
    observable to anyone, and it is stored rather than recomputed so the
    boundary cannot drift when a provider re-stamps an article.

    ``ingested_at`` is when *we* imported the row into this historical cache.
    It is not actual live receipt. The general cache query can reconstruct
    publicly observable history from ``observed_at``. A research packet applies
    ``ingested_at <= as_of`` so it only uses data actually cached then.
    """

    __tablename__ = "news_history_articles"

    provider: Mapped[SourceProvider] = mapped_column(
        pg_enum(SourceProvider, "source_provider"), nullable=False
    )
    provider_item_id: Mapped[str | None] = mapped_column(sa.Text)
    feed: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="unknown")
    """Which transport produced this row: ``alpaca:backfill``, ``alpaca:stream``.

    The provider says who; the feed says how.  Two rows can share a provider and
    still be different vintages of the same article, so the feed is part of the
    audit trail even though it is not part of identity.
    """

    headline: Mapped[str | None] = mapped_column(sa.Text)
    symbols: Mapped[list[str]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    """Every ticker Alpaca associated with the article, upper-cased."""

    event_type: Mapped[str | None] = mapped_column(sa.Text)
    """Copied from :class:`~stockbrain.db.models.sources.Event.event_type` so an
    exact ``(company, event_type)`` filter is one indexed lookup and stays
    correct even before the classifier has run (it is then NULL)."""

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id", ondelete="SET NULL")
    )
    """Archived event used only for historical duplicate grouping. Nullable
    when the article has no usable headline."""

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("sources.id", ondelete="SET NULL")
    )
    """Legacy link to a Source; new historical imports leave this NULL."""

    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    observed_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    content_hash: Mapped[str | None] = mapped_column(sa.String(64))
    raw_payload: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        # Idempotency: the provider's own article id is the artefact identity.
        sa.Index(
            "uq_news_history_articles_provider_item",
            "provider",
            "provider_item_id",
            unique=True,
            postgresql_where=sa.text("provider_item_id IS NOT NULL"),
        ),
        sa.Index("ix_news_history_articles_observed_at", "observed_at"),
        sa.Index(
            "ix_news_history_articles_company_event_observed",
            "company_id",
            "event_type",
            "observed_at",
        ),
        sa.Index("ix_news_history_articles_event_id", "event_id"),
        sa.Index("ix_news_history_articles_content_hash", "content_hash"),
    )


class NewsHistoryCandle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cached OHLCV bar, keyed by symbol, timeframe, start and feed.

    ``observed_at`` is the bar's *close* -- the instant the aggregate first
    existed -- not its start.  A point-in-time query at 09:30:30 must not be
    able to see the 09:30 bar that is still forming.
    """

    __tablename__ = "news_history_candles"

    symbol: Mapped[str] = mapped_column(sa.Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="1Min")
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="unknown")
    feed: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="unknown")

    bar_start: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    open: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    volume: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    trade_count: Mapped[int | None] = mapped_column(sa.Integer)
    vwap: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    raw_payload: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        # Idempotency: the same bar from the same feed is the same row.
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "bar_start",
            "feed",
            name="uq_news_history_candles_identity",
        ),
        sa.Index(
            "ix_news_history_candles_symbol_timeframe_start",
            "symbol",
            "timeframe",
            "bar_start",
        ),
        sa.Index("ix_news_history_candles_observed_at", "observed_at"),
    )


class NewsHistoryCoverage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Watermark and completeness for one dataset scope.

    ``scope_key`` is a symbol for candles, or :data:`DATASET_NEWS`'s ``GLOBAL``
    for the news firehose (and a company id when a caller scopes news to one
    company).  ``is_partial`` is the honest answer to "did we stop early?":
    a bounded run leaves it True and ``detail`` says which cap bit.
    """

    __tablename__ = "news_history_coverage"

    dataset: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(sa.Text, nullable=False)

    covered_start: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    covered_end: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    watermark_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    """Highest ``observed_at`` successfully cached for the scope.  Only ever
    moves forward; an older backfill cannot drag it back."""

    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    article_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    bar_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    request_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")

    is_partial: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    detail: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint("dataset", "scope_key", name="uq_news_history_coverage_scope"),
        sa.Index("ix_news_history_coverage_dataset", "dataset"),
    )


class NewsHistoryBackfillRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One explicit backfill attempt and the caps it consumed.

    A run is bounded by its ``limits`` JSONB (days, articles, bar pages, request
    count).  When a cap is reached the run finishes ``PARTIAL`` with ``bounded``
    set and a ``stop_reason`` naming the cap -- so "the history is incomplete"
    is a recorded fact with an owner, not an inference from missing rows.
    """

    __tablename__ = "news_history_backfill_runs"

    dataset: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=RUN_STATUS_RUNNING)

    requested_start: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    requested_end: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    limits: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    articles_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    bars_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    pages_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    requests_made: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")

    bounded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    stop_reason: Mapped[str | None] = mapped_column(sa.Text)
    watermark_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    started_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_news_history_backfill_runs_dataset_started", "dataset", "started_at"),
        sa.Index("ix_news_history_backfill_runs_status", "status"),
    )
