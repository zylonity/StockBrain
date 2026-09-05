"""Instrument resolution and market-data inspection.

Read-only, like every route in this application.  There are still **zero**
state-changing HTTP routes, and in particular nothing here can place, amend or
cancel an order: the only broker client reachable from this process is the
read-only metadata client.

The point of these endpoints is to make a refusal inspectable.  An AMBIGUOUS
resolution that a human cannot look at is indistinguishable from a bug, so the
model's ticker hint, the resolved instrument, the confidence and every rejected
alternative are all exposed side by side.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.api.dependencies import DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    AliasResponse,
    BrokerInstrumentResponse,
    InstrumentCandidateResponse,
    InstrumentSyncStatusResponse,
    MarketDataHealthResponse,
    PriceReactionResponse,
    QuoteResponse,
    ResolutionListResponse,
    ResolutionResponse,
)
from stockbrain.db.models.companies import (
    BrokerExchange,
    BrokerInstrument,
    BrokerWorkingSchedule,
    Company,
    CompanyAlias,
    EventCompanyImpact,
)
from stockbrain.db.models.sources import Event
from stockbrain.enums import (
    EXECUTION_GRADE_PRICE_SOURCES,
    Broker,
    CapabilityState,
    ResolutionStatus,
)
from stockbrain.errors import ProviderError
from stockbrain.market_data.base import quote_blockers
from stockbrain.market_data.reaction import PriceReactionCalculator
from stockbrain.market_data.sessions import (
    SessionVerdict,
    session_from_schedule,
    session_from_us_clock,
)

router = APIRouter(prefix="/api/v1", tags=["instruments"])


def _candidates(raw: list[dict[str, Any]]) -> list[InstrumentCandidateResponse]:
    """Rebuild stored alternatives, skipping anything that no longer parses.

    Alternatives are a JSONB snapshot taken when the resolution ran; a shape
    change must degrade the display, never 500 the endpoint that explains why
    something did not resolve.
    """
    parsed: list[InstrumentCandidateResponse] = []
    for item in raw:
        try:
            parsed.append(InstrumentCandidateResponse.model_validate(item))
        except ValueError:
            continue
    return parsed


def _to_resolution(
    impact: EventCompanyImpact,
    instrument: BrokerInstrument | None,
    company: Company | None,
    event_title: str | None,
) -> ResolutionResponse:
    return ResolutionResponse(
        impact_id=impact.id,
        event_id=impact.event_id,
        event_title=event_title,
        company_name_hint=impact.company_name_hint,
        model_ticker_hint=impact.ticker_hint,
        model_exchange_hint=impact.exchange_hint,
        status=impact.resolution_status.value,
        method=impact.resolution_method,
        confidence=impact.resolution_confidence,
        notes=impact.resolution_notes,
        resolved_at=impact.resolved_at,
        company_id=impact.company_id,
        company_name=company.name if company else None,
        broker_instrument_id=impact.broker_instrument_id,
        broker_ticker=instrument.broker_ticker if instrument else None,
        market_symbol=instrument.market_symbol if instrument else None,
        exchange=instrument.exchange if instrument else None,
        currency=instrument.currency if instrument else None,
        isin=instrument.isin if instrument else None,
        instrument_type=instrument.instrument_type if instrument else None,
        alternatives=_candidates(list(impact.resolution_alternatives or [])),
    )


@router.get("/instruments/resolutions", response_model=ResolutionListResponse)
async def list_resolutions(
    session: DbSession,
    resolution_status: Annotated[ResolutionStatus | None, Query()] = None,
    event_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResolutionListResponse:
    """Every company hint and what it resolved to.

    Defaults to all statuses so an operator's first view includes the
    unresolved ones -- those are the rows that need a human.
    """
    filters = []
    if resolution_status is not None:
        filters.append(EventCompanyImpact.resolution_status == resolution_status)
    if event_id is not None:
        filters.append(EventCompanyImpact.event_id == event_id)

    total = int(
        (
            await session.execute(
                sa.select(sa.func.count()).select_from(EventCompanyImpact).where(*filters)
            )
        ).scalar_one()
    )

    # Labelled "tally" rather than "count": `Row.count` is a tuple method, and
    # a label that shadows it silently yields the bound method instead.
    counts = {
        str(row.resolution_status.value): int(row.tally)
        for row in (
            await session.execute(
                sa.select(
                    EventCompanyImpact.resolution_status,
                    sa.func.count().label("tally"),
                ).group_by(EventCompanyImpact.resolution_status)
            )
        ).all()
    }

    rows = (
        await session.execute(
            sa.select(EventCompanyImpact, BrokerInstrument, Company, Event.title)
            .outerjoin(
                BrokerInstrument, BrokerInstrument.id == EventCompanyImpact.broker_instrument_id
            )
            .outerjoin(Company, Company.id == EventCompanyImpact.company_id)
            .outerjoin(Event, Event.id == EventCompanyImpact.event_id)
            .where(*filters)
            .order_by(EventCompanyImpact.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return ResolutionListResponse(
        items=[
            _to_resolution(impact, instrument, company, title)
            for impact, instrument, company, title in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        counts_by_status=counts,
    )


@router.get("/instruments/resolutions/{impact_id}", response_model=ResolutionResponse)
async def get_resolution(impact_id: uuid.UUID, session: DbSession) -> ResolutionResponse:
    row = (
        await session.execute(
            sa.select(EventCompanyImpact, BrokerInstrument, Company, Event.title)
            .outerjoin(
                BrokerInstrument, BrokerInstrument.id == EventCompanyImpact.broker_instrument_id
            )
            .outerjoin(Company, Company.id == EventCompanyImpact.company_id)
            .outerjoin(Event, Event.id == EventCompanyImpact.event_id)
            .where(EventCompanyImpact.id == impact_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resolution not found")
    impact, instrument, company, title = row
    return _to_resolution(impact, instrument, company, title)


@router.get("/instruments", response_model=list[BrokerInstrumentResponse])
async def list_instruments(
    session: DbSession,
    search: Annotated[str | None, Query(max_length=100)] = None,
    isin: Annotated[str | None, Query(max_length=12)] = None,
    include_inactive: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BrokerInstrumentResponse]:
    """Browse the synced broker universe."""
    statement = sa.select(BrokerInstrument)
    if not include_inactive:
        statement = statement.where(BrokerInstrument.is_active.is_(True))
    if isin:
        statement = statement.where(BrokerInstrument.isin == isin.strip().upper())
    if search:
        pattern = f"%{search.strip().lower()}%"
        statement = statement.where(
            sa.or_(
                sa.func.lower(BrokerInstrument.name).like(pattern),
                sa.func.lower(BrokerInstrument.broker_ticker).like(pattern),
                sa.func.lower(BrokerInstrument.market_symbol).like(pattern),
            )
        )
    rows = (
        (await session.execute(statement.order_by(BrokerInstrument.broker_ticker).limit(limit)))
        .scalars()
        .all()
    )
    return [
        BrokerInstrumentResponse(
            id=row.id,
            broker=row.broker.value,
            broker_ticker=row.broker_ticker,
            name=row.name,
            short_name=row.short_name,
            market_symbol=row.market_symbol,
            market_code=row.market_code,
            exchange=row.exchange,
            currency=row.currency,
            isin=row.isin,
            instrument_type=row.instrument_type,
            extended_hours=row.extended_hours,
            min_trade_quantity=row.min_trade_quantity,
            max_open_quantity=row.max_open_quantity,
            working_schedule_id=row.working_schedule_id,
            added_on=row.added_on,
            is_active=row.is_active,
            company_id=row.company_id,
            last_refreshed_at=row.last_refreshed_at,
        )
        for row in rows
    ]


@router.get("/instruments/sync-status", response_model=InstrumentSyncStatusResponse)
async def instrument_sync_status(
    session: DbSession, services: ServicesDep
) -> InstrumentSyncStatusResponse:
    """Whether the broker universe has been synced, and how completely."""
    totals = (
        await session.execute(
            sa.select(
                sa.func.count().label("total"),
                sa.func.count().filter(BrokerInstrument.is_active.is_(True)).label("active"),
                sa.func.count().filter(BrokerInstrument.isin.is_not(None)).label("with_isin"),
                sa.func.count()
                .filter(BrokerInstrument.exchange.is_not(None))
                .label("with_exchange"),
                sa.func.max(BrokerInstrument.last_refreshed_at).label("last_refreshed_at"),
            ).where(BrokerInstrument.broker == Broker.TRADING212)
        )
    ).one()

    exchanges = int(
        (await session.execute(sa.select(sa.func.count()).select_from(BrokerExchange))).scalar_one()
    )
    schedules = int(
        (
            await session.execute(sa.select(sa.func.count()).select_from(BrokerWorkingSchedule))
        ).scalar_one()
    )

    client = services.t212_metadata if services else None
    return InstrumentSyncStatusResponse(
        broker=Broker.TRADING212.value,
        configured=client is not None,
        instruments_total=int(totals.total),
        instruments_active=int(totals.active),
        with_isin=int(totals.with_isin),
        with_exchange=int(totals.with_exchange),
        exchanges=exchanges,
        working_schedules=schedules,
        last_refreshed_at=totals.last_refreshed_at,
        rate_limit=client.rate_limit_snapshot() if client else {},
    )


@router.get("/aliases", response_model=list[AliasResponse])
async def list_aliases(
    session: DbSession,
    company_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[AliasResponse]:
    """The curated alias table, so every manual mapping is inspectable."""
    statement = sa.select(CompanyAlias, Company.name).outerjoin(
        Company, Company.id == CompanyAlias.company_id
    )
    if company_id is not None:
        statement = statement.where(CompanyAlias.company_id == company_id)
    rows = (
        await session.execute(statement.order_by(CompanyAlias.alias_normalized).limit(limit))
    ).all()
    return [
        AliasResponse(
            id=alias.id,
            company_id=alias.company_id,
            company_name=name,
            alias=alias.alias,
            alias_normalized=alias.alias_normalized,
            alias_type=alias.alias_type.value,
            exchange=alias.exchange,
            currency=alias.currency,
            isin=alias.isin,
            is_authoritative=alias.is_authoritative,
            confidence=alias.confidence,
            source=alias.source,
            notes=alias.notes,
        )
        for alias, name in rows
    ]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@router.get("/market-data/health", response_model=MarketDataHealthResponse)
async def market_data_health(
    services: ServicesDep, settings: SettingsDep
) -> MarketDataHealthResponse:
    """What the configured market-data provider can actually reach.

    Reports the probed capability, not the configuration's intent: a deployment
    asking for SIP without a SIP subscription reads ENTITLEMENT_MISSING here
    rather than looking healthy until the first proposal needs a price.
    """
    if services is None or services.market_data is None:
        return MarketDataHealthResponse(
            configured=False,
            state=CapabilityState.DISABLED.value,
            max_quote_age_seconds=settings.market_data_max_quote_age_seconds,
            blockers=["Alpaca market data is not configured"],
        )
    capability = await services.market_data.capability()
    return MarketDataHealthResponse(
        provider=capability.provider,
        configured=True,
        state=capability.state.value,
        feed=capability.feed,
        detail=capability.detail,
        checked_at=capability.checked_at,
        realtime_pricing_usable=capability.realtime_pricing_usable,
        probe_symbol=capability.probe_symbol,
        probe_quote_age_ms=capability.probe_quote_age_ms,
        probe_quote_stale=capability.probe_quote_stale,
        max_quote_age_seconds=settings.market_data_max_quote_age_seconds,
        blockers=list(capability.blockers),
    )


@router.get("/market-data/quote/{symbol}", response_model=QuoteResponse)
async def get_quote(symbol: str, services: ServicesDep, settings: SettingsDep) -> QuoteResponse:
    """One live quote, with its age and whether it could ever size an order."""
    if services is None or services.market_data is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data provider is not configured",
        )
    provider = services.market_data
    try:
        quote = await provider.latest_quote(symbol)
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    capability = await provider.capability()
    blockers = quote_blockers(
        quote, capability, max_age_seconds=settings.market_data_max_quote_age_seconds
    )
    return QuoteResponse(
        symbol=quote.symbol,
        provider=quote.provider,
        feed=quote.feed,
        price_source=quote.price_source.value,
        price=str(quote.price) if quote.price is not None else None,
        bid=str(quote.bid) if quote.bid is not None else None,
        ask=str(quote.ask) if quote.ask is not None else None,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        currency=quote.currency,
        provider_timestamp=quote.provider_timestamp,
        received_at=quote.received_at,
        quote_age_ms=quote.age_ms,
        is_two_sided=quote.is_two_sided,
        execution_grade=quote.price_source in EXECUTION_GRADE_PRICE_SOURCES,
        sizing_blockers=blockers,
    )


@router.get("/events/{event_id}/price-reaction", response_model=list[PriceReactionResponse])
async def event_price_reaction(
    event_id: uuid.UUID, session: DbSession, services: ServicesDep
) -> list[PriceReactionResponse]:
    """How much each resolved security has moved since the event became public.

    Context only.  Nothing in Phase 4 reads this number to make a decision, and
    it carries its own provenance so a later phase cannot start trusting it
    without seeing how it was measured.
    """
    if services is None or services.market_data is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="market data provider is not configured",
        )

    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    rows = (
        await session.execute(
            sa.select(EventCompanyImpact, BrokerInstrument)
            .join(BrokerInstrument, BrokerInstrument.id == EventCompanyImpact.broker_instrument_id)
            .where(
                EventCompanyImpact.event_id == event_id,
                EventCompanyImpact.resolution_status == ResolutionStatus.RESOLVED,
            )
        )
    ).all()

    # The event's own timestamp is what "became public" means; first_seen_at is
    # when *we* saw it, which is later and would understate the move.
    event_time: dt.datetime = event.event_time or event.first_seen_at
    calculator = PriceReactionCalculator(services.market_data)

    responses: list[PriceReactionResponse] = []
    for _impact, instrument in rows:
        if not instrument.market_symbol:
            continue
        verdict = await _session_verdict(session, instrument, event_time)
        reaction = await calculator.compute(instrument.market_symbol, event_time, session=verdict)
        responses.append(PriceReactionResponse.model_validate(reaction.as_dict()))
    return responses


async def _session_verdict(
    session: AsyncSession, instrument: BrokerInstrument, moment: dt.datetime
) -> SessionVerdict | None:
    """Prefer the broker's own holiday-aware schedule; fall back to the clock."""
    if instrument.working_schedule_id is not None:
        schedule = (
            await session.execute(
                sa.select(BrokerWorkingSchedule).where(
                    BrokerWorkingSchedule.broker == instrument.broker,
                    BrokerWorkingSchedule.provider_schedule_id == instrument.working_schedule_id,
                )
            )
        ).scalar_one_or_none()
        if schedule is not None:
            verdict = session_from_schedule(list(schedule.time_events or []), moment)
            if verdict.source != "none":
                return verdict
    if (instrument.market_code or "").upper() == "US":
        return session_from_us_clock(moment)
    return None
