"""Companies, their broker instruments, and per-event company impact records.

The company -> broker instrument mapping is the correctness boundary of the whole
system: an LLM may say "Apple", but only a verified :class:`BrokerInstrument`
row -- matched by ISIN where possible -- may ever reach an order request.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import AliasType, Broker, ImpactDirection, ResolutionStatus

if TYPE_CHECKING:
    from stockbrain.db.models.sources import Event

__all__ = [
    "BrokerExchange",
    "BrokerInstrument",
    "BrokerWorkingSchedule",
    "Company",
    "CompanyAlias",
    "EventCompanyImpact",
]


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name_key: Mapped[str | None] = mapped_column(sa.Text)
    """Normalised name, so a lookup by company name does not depend on how the
    name happened to be punctuated when the row was written."""

    primary_symbol: Mapped[str | None] = mapped_column(sa.Text)
    """Market-data symbol (e.g. ``AAPL``).  Never a broker ticker."""

    exchange: Mapped[str | None] = mapped_column(sa.Text)
    country: Mapped[str | None] = mapped_column(sa.String(2))
    isin: Mapped[str | None] = mapped_column(sa.String(12))
    cik: Mapped[str | None] = mapped_column(sa.String(10))
    sector: Mapped[str | None] = mapped_column(sa.Text)
    industry: Mapped[str | None] = mapped_column(sa.Text)
    aliases: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))
    is_watchlisted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    instruments: Mapped[list[BrokerInstrument]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    alias_rows: Mapped[list[CompanyAlias]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.Index(
            "uq_companies_isin", "isin", unique=True, postgresql_where=sa.text("isin IS NOT NULL")
        ),
        sa.Index(
            "uq_companies_cik", "cik", unique=True, postgresql_where=sa.text("cik IS NOT NULL")
        ),
        sa.Index("ix_companies_primary_symbol", "primary_symbol"),
        sa.Index("ix_companies_name", "name"),
        sa.Index("ix_companies_name_key", "name_key"),
    )


class CompanyAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Deterministic lookup table used to resolve LLM-supplied names.

    Curated manually for ambiguous cases the model gets wrong on its own
    (``Alphabet`` -> GOOGL/GOOG, ``Berkshire`` -> BRK.A/BRK.B, ADRs and dual
    listings).  A low-confidence alias must not be sufficient to size an order.
    """

    __tablename__ = "company_aliases"

    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alias_normalized: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="MANUAL")
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="1.0")

    alias_type: Mapped[AliasType] = mapped_column(
        pg_enum(AliasType, "alias_type"),
        nullable=False,
        default=AliasType.COMMON,
        server_default=AliasType.COMMON.value,
    )
    exchange: Mapped[str | None] = mapped_column(sa.Text)
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    """Listing scope.  A ``LISTING`` alias with an exchange and currency maps a
    name to one specific listing, which is how "Alphabet class A" can exist
    alongside "Alphabet class C" without making the bare name "Alphabet"
    ambiguous by accident."""

    isin: Mapped[str | None] = mapped_column(sa.String(12))
    is_authoritative: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    """An authoritative alias is allowed to decide a resolution on its own.  Two
    of them claiming the same name in the same scope for different companies is
    a contradiction, and ``uq_company_aliases_authoritative_scope`` refuses it
    at the database rather than letting the resolver pick a winner."""

    notes: Mapped[str | None] = mapped_column(sa.Text)

    company: Mapped[Company] = relationship(back_populates="alias_rows")

    __table_args__ = (
        sa.UniqueConstraint(
            "alias_normalized", "company_id", name="uq_company_aliases_alias_company"
        ),
        sa.Index("ix_company_aliases_alias_normalized", "alias_normalized"),
        sa.Index(
            "uq_company_aliases_authoritative_scope",
            sa.text("alias_normalized"),
            sa.text("alias_type"),
            sa.text("coalesce(exchange, '')"),
            sa.text("coalesce(currency, '')"),
            unique=True,
            postgresql_where=sa.text("is_authoritative"),
        ),
    )


class BrokerExchange(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An exchange as the broker enumerates it.

    Trading 212 does not put an exchange on an instrument at all: the instrument
    carries a ``workingScheduleId``, and only ``/equity/metadata/exchanges``
    says which exchange that schedule belongs to.  So the exchange of an
    instrument is *derived* from this table, and an instrument whose schedule is
    not in it has no known exchange rather than a guessed one.
    """

    __tablename__ = "broker_exchanges"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    provider_exchange_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    name: Mapped[str | None] = mapped_column(sa.Text)
    raw_metadata: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    last_refreshed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    schedules: Mapped[list[BrokerWorkingSchedule]] = relationship(
        back_populates="exchange", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "broker", "provider_exchange_id", name="uq_broker_exchanges_broker_provider_exchange_id"
        ),
    )


class BrokerWorkingSchedule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One trading schedule of one exchange, with its time events.

    The events are stored verbatim because they are the only holiday-aware
    session information the system has: deriving "was this event pre-market?"
    from a hard-coded clock would silently be wrong on every exchange holiday.
    """

    __tablename__ = "broker_working_schedules"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    provider_schedule_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    exchange_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("broker_exchanges.id", ondelete="CASCADE"), nullable=False
    )
    time_events: Mapped[list[JSONDict]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    last_refreshed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    exchange: Mapped[BrokerExchange] = relationship(back_populates="schedules")

    __table_args__ = (
        sa.UniqueConstraint(
            "broker",
            "provider_schedule_id",
            name="uq_broker_working_schedules_broker_provider_schedule_id",
        ),
    )


class BrokerInstrument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tradable instrument as the broker defines it.

    Trading 212 identifies instruments with its own ticker format (``AAPL_US_EQ``)
    which must never be assumed equal to the market-data symbol.
    """

    __tablename__ = "broker_instruments"

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str | None] = mapped_column(sa.Text)
    short_name: Mapped[str | None] = mapped_column(sa.Text)
    isin: Mapped[str | None] = mapped_column(sa.String(12))
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    instrument_type: Mapped[str | None] = mapped_column(sa.Text)
    extended_hours: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    min_trade_quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(28, 10))
    max_open_quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(28, 10))
    working_schedule_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    added_on: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    raw_metadata: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    last_refreshed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    exchange: Mapped[str | None] = mapped_column(sa.Text)
    """Derived from ``working_schedule_id`` via :class:`BrokerWorkingSchedule`.
    NULL when the schedule is unknown -- never guessed from the ticker."""

    exchange_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    market_symbol: Mapped[str | None] = mapped_column(sa.Text)
    """Market-data symbol, derived from ``shortName`` (or the ticker prefix).

    Derived, not authoritative: it is a *lookup key* for a market-data provider
    and must never be sent to the broker.  ``broker_ticker`` is the only
    identity an order may ever carry."""

    market_code: Mapped[str | None] = mapped_column(sa.Text)
    """Venue code parsed out of the broker ticker (``AAPL_US_EQ`` -> ``US``)."""

    name_key: Mapped[str | None] = mapped_column(sa.Text)
    """Normalised company name, for name-based candidate generation."""

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    """False once a full sync stops returning the instrument.  Rows are never
    deleted: a proposal or an execution attempt may reference one forever."""

    last_seen_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    company: Mapped[Company | None] = relationship(back_populates="instruments")

    __table_args__ = (
        sa.UniqueConstraint(
            "broker", "broker_ticker", name="uq_broker_instruments_broker_broker_ticker"
        ),
        sa.Index("ix_broker_instruments_isin", "isin"),
        sa.Index("ix_broker_instruments_company_id", "company_id"),
        sa.Index("ix_broker_instruments_broker_isin", "broker", "isin"),
        sa.Index("ix_broker_instruments_broker_market_symbol", "broker", "market_symbol"),
        sa.Index("ix_broker_instruments_broker_name_key", "broker", "name_key"),
        sa.Index("ix_broker_instruments_working_schedule_id", "working_schedule_id"),
    )


class EventCompanyImpact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Classifier output linking an event to a company, with materiality.

    ``company_id`` stays NULL until instrument resolution succeeds; the raw
    name/ticker hints the model produced are preserved either way so a failed
    resolution is auditable.
    """

    __tablename__ = "event_company_impacts"

    event_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    company_name_hint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    company_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    """Normalised company name, unique per event.

    This is what makes classification idempotent at the database level: running
    the same CLASSIFY_EVENT job twice cannot create a second impact row for the
    same company, because `uq_event_company_impacts_event_id_company_key` will
    not allow it."""

    ticker_hint: Mapped[str | None] = mapped_column(sa.Text)
    exchange_hint: Mapped[str | None] = mapped_column(sa.Text)

    direction: Mapped[ImpactDirection] = mapped_column(
        pg_enum(ImpactDirection, "impact_direction"),
        nullable=False,
        default=ImpactDirection.UNKNOWN,
    )
    relationship_type: Mapped[str | None] = mapped_column(sa.Text)
    impact_path: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="unknown")
    """direct | indirect | unknown.

    Separate from materiality: a large indirect effect and a small direct one are
    different things, and sizing should be able to tell them apart."""

    materiality_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    explanation: Mapped[str | None] = mapped_column(sa.Text)

    broker_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        # Named explicitly: the naming convention would generate a 64-character
        # identifier, one over PostgreSQL's limit, and a silently truncated name
        # is a constraint a migration can no longer drop by name.
        sa.ForeignKey(
            "broker_instruments.id",
            ondelete="SET NULL",
            name="fk_event_company_impacts_broker_instrument_id",
        )
    )
    """The *only* executable identity.  Populated exclusively by the resolver
    from synced broker metadata; ``ticker_hint`` never becomes this."""

    resolution_status: Mapped[ResolutionStatus] = mapped_column(
        pg_enum(ResolutionStatus, "resolution_status"),
        nullable=False,
        default=ResolutionStatus.PENDING,
        server_default=ResolutionStatus.PENDING.value,
    )
    resolution_confidence: Mapped[float | None] = mapped_column(sa.Float)
    resolution_method: Mapped[str | None] = mapped_column(sa.Text)
    resolution_notes: Mapped[str | None] = mapped_column(sa.Text)
    resolution_alternatives: Mapped[list[JSONDict]] = mapped_column(
        pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    """Every other listing that matched, so an AMBIGUOUS result explains itself
    instead of merely refusing."""

    resolved_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="company_impacts")
    company: Mapped[Company | None] = relationship()

    __table_args__ = (
        sa.UniqueConstraint(
            "event_id", "company_key", name="uq_event_company_impacts_event_id_company_key"
        ),
        sa.Index("ix_event_company_impacts_event_id", "event_id"),
        sa.Index("ix_event_company_impacts_company_id", "company_id"),
        sa.Index("ix_event_company_impacts_resolution_status", "resolution_status"),
        sa.Index("ix_event_company_impacts_broker_instrument_id", "broker_instrument_id"),
        sa.CheckConstraint(
            "materiality_score >= 0 AND materiality_score <= 1", name="materiality_range"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
