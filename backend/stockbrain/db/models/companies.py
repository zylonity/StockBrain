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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import Broker, ImpactDirection

if TYPE_CHECKING:
    from stockbrain.db.models.sources import Event

__all__ = ["BrokerInstrument", "Company", "CompanyAlias", "EventCompanyImpact"]


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
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

    company: Mapped[Company] = relationship(back_populates="alias_rows")

    __table_args__ = (
        sa.UniqueConstraint(
            "alias_normalized", "company_id", name="uq_company_aliases_alias_company"
        ),
        sa.Index("ix_company_aliases_alias_normalized", "alias_normalized"),
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

    company: Mapped[Company | None] = relationship(back_populates="instruments")

    __table_args__ = (
        sa.UniqueConstraint(
            "broker", "broker_ticker", name="uq_broker_instruments_broker_broker_ticker"
        ),
        sa.Index("ix_broker_instruments_isin", "isin"),
        sa.Index("ix_broker_instruments_company_id", "company_id"),
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
    ticker_hint: Mapped[str | None] = mapped_column(sa.Text)
    exchange_hint: Mapped[str | None] = mapped_column(sa.Text)

    direction: Mapped[ImpactDirection] = mapped_column(
        pg_enum(ImpactDirection, "impact_direction"),
        nullable=False,
        default=ImpactDirection.UNKNOWN,
    )
    relationship_type: Mapped[str | None] = mapped_column(sa.Text)
    materiality_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    explanation: Mapped[str | None] = mapped_column(sa.Text)

    resolution_confidence: Mapped[float | None] = mapped_column(sa.Float)
    resolution_method: Mapped[str | None] = mapped_column(sa.Text)
    resolution_notes: Mapped[str | None] = mapped_column(sa.Text)

    event: Mapped[Event] = relationship(back_populates="company_impacts")
    company: Mapped[Company | None] = relationship()

    __table_args__ = (
        sa.Index("ix_event_company_impacts_event_id", "event_id"),
        sa.Index("ix_event_company_impacts_company_id", "company_id"),
        sa.CheckConstraint(
            "materiality_score >= 0 AND materiality_score <= 1", name="materiality_range"
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
