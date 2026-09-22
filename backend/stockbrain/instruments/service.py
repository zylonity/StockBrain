"""``RESOLVE_CANDIDATES``: turn classifier hints into verified instruments.

The pipeline this closes is:

    classified event -> affected-company hint -> RESOLVE_CANDIDATES
        -> verified company (when an ISIN identifies one)
        -> verified Trading 212 instrument, or a visible unresolved state

Idempotency is layered the same way classification's is, because at-least-once
delivery guarantees this job runs twice eventually:

* the queue's ``uq_jobs_dedupe_key_active`` allows one pending resolve per event;
* each impact row is updated by primary key under ``SELECT ... FOR UPDATE``, so
  two workers serialise rather than interleave;
* companies are created by ``INSERT ... ON CONFLICT (isin)``, which is a
  database-level guarantee rather than a check-then-insert race.

A resolution is recomputed on every run and the row is simply overwritten with
the same verdict, so re-running is a no-op in effect as well as in form.  There
is no path in this module that writes ``broker_instrument_id`` from anything but
a ``broker_instruments`` primary key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.sources import EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import Broker, ResolutionStatus
from stockbrain.instruments.normalize import instrument_name_key, normalize_isin, normalize_ticker
from stockbrain.instruments.resolver import (
    InstrumentResolver,
    ResolutionRequest,
    ResolutionResult,
)
from stockbrain.intelligence.normalize import company_key
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["ResolutionRunResult", "ResolutionService"]

log = get_logger(__name__)


@dataclass(slots=True)
class ResolutionRunResult:
    event_id: uuid.UUID
    considered: int = 0
    resolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    unsupported: int = 0
    companies_created: int = 0
    statuses: dict[str, int] = field(default_factory=dict)


class ResolutionService:
    """Resolves every company impact on an event."""

    def __init__(self, database: Database, *, broker: Broker = Broker.TRADING212) -> None:
        self._database = database
        self._broker = broker

    async def resolve_event(self, event_id: uuid.UUID) -> ResolutionRunResult:
        """Resolve all impacts of one event.  Safe to call repeatedly."""
        result = ResolutionRunResult(event_id=event_id)

        async with self._database.session() as session:
            impact_ids = list(
                (
                    await session.execute(
                        sa.select(EventCompanyImpact.id)
                        .where(EventCompanyImpact.event_id == event_id)
                        .order_by(EventCompanyImpact.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )

        for impact_id in impact_ids:
            outcome = await self.resolve_impact(impact_id)
            if outcome is None:
                continue
            result.considered += 1
            result.statuses[outcome.status.value] = result.statuses.get(outcome.status.value, 0) + 1
            if outcome.status is ResolutionStatus.RESOLVED:
                result.resolved += 1
            elif outcome.status is ResolutionStatus.AMBIGUOUS:
                result.ambiguous += 1
            elif outcome.status is ResolutionStatus.UNSUPPORTED:
                result.unsupported += 1
            else:
                result.not_found += 1
            METRICS.inc(
                "stockbrain_instrument_resolutions_total",
                labels={"status": outcome.status.value},
            )

        log.info(
            "resolve_candidates_complete",
            event_id=str(event_id),
            considered=result.considered,
            resolved=result.resolved,
            ambiguous=result.ambiguous,
            not_found=result.not_found,
            unsupported=result.unsupported,
        )
        return result

    async def resolve_impact(self, impact_id: uuid.UUID) -> ResolutionResult | None:
        """Resolve one impact row and persist the verdict.

        The row is locked for the duration, so a concurrent worker running the
        same job blocks and then writes the identical verdict rather than
        interleaving a half-written resolution.
        """
        async with self._database.transaction() as session:
            impact = (
                await session.execute(
                    sa.select(EventCompanyImpact)
                    .where(EventCompanyImpact.id == impact_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if impact is None:
                return None

            resolver = InstrumentResolver(session)
            source_metadata = await self._source_metadata(session, impact.event_id)
            outcome = await resolver.resolve(
                ResolutionRequest(
                    name_hint=impact.company_name_hint,
                    ticker_hint=impact.ticker_hint,
                    exchange_hint=impact.exchange_hint,
                    isin_hint=self._isin_hint_for_impact(impact, source_metadata),
                    broker=self._broker,
                )
            )

            company_id = outcome.company_id
            if outcome.is_resolved and outcome.broker_instrument_id is not None:
                company_id = await self._link_company(session, outcome.broker_instrument_id)

            impact.broker_instrument_id = (
                outcome.broker_instrument_id if outcome.is_resolved else None
            )
            impact.company_id = company_id if outcome.is_resolved else None
            impact.resolution_status = outcome.status
            impact.resolution_method = outcome.method.value
            impact.resolution_confidence = outcome.confidence if outcome.is_resolved else None
            impact.resolution_notes = outcome.notes
            impact.resolution_alternatives = outcome.alternatives_as_json()
            impact.resolved_at = utcnow()
            impact.updated_at = utcnow()
            await session.flush()

        return outcome

    # ------------------------------------------------------------------
    @staticmethod
    async def _source_metadata(session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any]:
        """The primary source's provider metadata for one event."""
        stmt = (
            sa.select(Source.provider_metadata)
            .join(EventSourceLink, EventSourceLink.source_id == Source.id)
            .where(EventSourceLink.event_id == event_id)
            .order_by(
                sa.case((EventSourceLink.relationship_type == "PRIMARY", 0), else_=1),
                Source.received_at.asc(),
            )
            .limit(1)
        )
        metadata = (await session.execute(stmt)).scalar_one_or_none()
        return dict(metadata or {})

    @staticmethod
    def _isin_hint_for_impact(impact: EventCompanyImpact, metadata: dict[str, Any]) -> str | None:
        """The source's ISIN, but only for an impact the source actually names.

        An impact row has no ISIN column, so the hand-off happens here: the
        document's own identity travels with the impact when the impact's
        ticker or normalised company key matches what the feed printed.
        """
        isin = normalize_isin(str(metadata.get("isin") or ""))
        if not isin:
            return None
        raw_symbols = metadata.get("symbols")
        document_symbol = (
            normalize_ticker(str(raw_symbols[0]))
            if isinstance(raw_symbols, list) and len(raw_symbols) == 1
            else ""
        )
        ticker = normalize_ticker(impact.ticker_hint)
        if document_symbol and ticker and ticker == document_symbol:
            return isin
        document_key = company_key(str(metadata.get("company_name") or ""))
        if document_key and document_key == impact.company_key:
            return isin
        return None

    async def _link_company(
        self, session: AsyncSession, broker_instrument_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Attach the resolved instrument to a company, creating one if needed.

        A company is created **only** from an ISIN.  ISIN is the one identifier
        that is globally unique per security, and ``uq_companies_isin`` turns
        "do not create a duplicate company" into a database guarantee rather
        than a hopeful lookup.  An instrument without an ISIN resolves normally
        but stays company-less: inventing a company from a ticker would create
        exactly the duplicates this phase must not produce.
        """
        instrument = await session.get(BrokerInstrument, broker_instrument_id)
        if instrument is None:  # pragma: no cover - just selected under lock
            return None
        if instrument.company_id is not None:
            return instrument.company_id

        isin = normalize_isin(instrument.isin)
        if not isin:
            return None

        now = utcnow()
        statement = pg_insert(Company).values(
            name=instrument.name or instrument.broker_ticker,
            name_key=instrument_name_key(instrument.name) or None,
            primary_symbol=normalize_ticker(instrument.market_symbol) or None,
            exchange=instrument.exchange,
            isin=isin,
            created_at=now,
            updated_at=now,
        )
        # DO UPDATE rather than DO NOTHING so the statement always returns the
        # id, whether this call created the row or found it already there.
        upsert = statement.on_conflict_do_update(
            index_where=sa.text("isin IS NOT NULL"),
            index_elements=[Company.isin],
            set_={"updated_at": now},
        ).returning(Company.id)
        company_id = uuid.UUID(str((await session.execute(upsert)).scalar_one()))

        instrument.company_id = company_id
        instrument.updated_at = now
        return company_id
