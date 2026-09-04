"""Raw source documents and the canonical events derived from them.

A *source* is one retrieved artefact: an Alpaca news article, an SEC filing, a
Firecrawl search result.  Sources are immutable evidence and are kept verbatim
for audit.

An *event* is the deduplicated real-world occurrence that one or more sources
describe.  Research runs are attached to events, not to articles, so a story
syndicated across ten outlets triggers one analysis.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import EventSourceRelationship, EventStatus, SourceProvider

if TYPE_CHECKING:
    from stockbrain.db.models.companies import EventCompanyImpact
    from stockbrain.db.models.research import ResearchRun

__all__ = ["Event", "EventSourceLink", "Source"]


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sources"

    provider: Mapped[SourceProvider] = mapped_column(
        pg_enum(SourceProvider, "source_provider"), nullable=False
    )
    provider_item_id: Mapped[str | None] = mapped_column(sa.Text)
    """Stable identifier from the provider: Alpaca article id, SEC accession number."""

    canonical_url: Mapped[str | None] = mapped_column(sa.Text)
    original_url: Mapped[str | None] = mapped_column(sa.Text)
    source_name: Mapped[str | None] = mapped_column(sa.Text)
    source_category: Mapped[str | None] = mapped_column(sa.Text)
    """Transparent trust category (REGULATOR, ISSUER, NEWSWIRE, PRESS, UNKNOWN).

    Kept as data rather than baked into an LLM prompt (spec section 37)."""

    headline: Mapped[str | None] = mapped_column(sa.Text)
    author: Mapped[str | None] = mapped_column(sa.Text)

    published_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    updated_at_source: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    received_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    raw_content: Mapped[str | None] = mapped_column(sa.Text)
    """Verbatim provider payload body.  Never rendered without sanitisation."""

    normalized_text: Mapped[str | None] = mapped_column(sa.Text)
    """Plain-text extraction used for hashing and LLM input."""

    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """SHA-256 of the normalised headline + body."""

    provider_metadata: Mapped[JSONDict] = mapped_column(
        "metadata", nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    events: Mapped[list[EventSourceLink]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.Index(
            "uq_sources_provider_item",
            "provider",
            "provider_item_id",
            unique=True,
            postgresql_where=sa.text("provider_item_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_sources_canonical_url_hash",
            "canonical_url",
            "content_hash",
            unique=True,
            postgresql_where=sa.text("canonical_url IS NOT NULL"),
        ),
        sa.Index("ix_sources_content_hash", "content_hash"),
        sa.Index("ix_sources_received_at", "received_at"),
        sa.Index("ix_sources_published_at", "published_at"),
    )


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "events"

    event_type: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """SHA-256 of the normalised title, for deterministic event grouping.

    Lets a story syndicated across outlets attach to one event with an indexed
    lookup instead of a scan. Not unique: the same headline recurring outside the
    match window is a genuinely new event."""

    summary: Mapped[str | None] = mapped_column(sa.Text)

    first_seen_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    event_time: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    """When the underlying event happened, as opposed to when we saw it."""

    importance_score: Mapped[float | None] = mapped_column(sa.Float)
    novelty_score: Mapped[float | None] = mapped_column(sa.Float)
    confidence_score: Mapped[float | None] = mapped_column(sa.Float)
    candidate_score: Mapped[float | None] = mapped_column(sa.Float)
    """Deterministic pre-research ranking (spec section 36).  Not a trading signal."""

    status: Mapped[EventStatus] = mapped_column(
        pg_enum(EventStatus, "event_status"),
        nullable=False,
        default=EventStatus.NEW,
        server_default=EventStatus.NEW.value,
    )

    classifier_model: Mapped[str | None] = mapped_column(sa.Text)
    classifier_prompt_version: Mapped[str | None] = mapped_column(sa.Text)
    classifier_output: Mapped[JSONDict | None] = mapped_column()
    topics: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    sources: Mapped[list[EventSourceLink]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    company_impacts: Mapped[list[EventCompanyImpact]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    research_runs: Mapped[list[ResearchRun]] = relationship(back_populates="event")

    __table_args__ = (
        sa.Index("ix_events_status_first_seen", "status", "first_seen_at"),
        sa.Index("ix_events_title_hash_first_seen", "title_hash", "first_seen_at"),
        sa.Index("ix_events_importance", "importance_score"),
        sa.CheckConstraint(
            "importance_score IS NULL OR (importance_score >= 0 AND importance_score <= 1)",
            name="importance_range",
        ),
        sa.CheckConstraint(
            "novelty_score IS NULL OR (novelty_score >= 0 AND novelty_score <= 1)",
            name="novelty_range",
        ),
    )


class EventSourceLink(Base):
    """Many-to-many between events and the sources that evidence them."""

    __tablename__ = "event_sources"

    event_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    relationship_type: Mapped[EventSourceRelationship] = mapped_column(
        pg_enum(EventSourceRelationship, "event_source_relationship"),
        nullable=False,
        default=EventSourceRelationship.PRIMARY,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    event: Mapped[Event] = relationship(back_populates="sources")
    source: Mapped[Source] = relationship(back_populates="events")
