"""Fetch and judge a quote at the moment a decision is made.

There is deliberately **no cache and no fallback**.  Every call reaches the
provider, and a failure produces ``(None, reason)`` rather than the last quote
that worked.  A proposal built on a remembered price is a proposal about a
market that no longer exists, and the whole point of Phase 4's provenance
columns was to make "how old is this number?" answerable rather than assumed.

The assessment bundles five independent facts, because a price is only usable
when all five hold:

* the provider is entitled and answering (``ProviderCapability``);
* the price source is execution-grade (``EXECUTION_GRADE_PRICE_SOURCES``);
* the quote is young enough;
* the book is two-sided and no wider than the configured ceiling;
* the instrument's own exchange is in a permitted session.

Trading 212 prices and yfinance can never satisfy the second one, so neither can
become an execution reference by being the only number available.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, BrokerWorkingSchedule
from stockbrain.errors import ProviderError
from stockbrain.market_data.base import MarketDataProvider, provider_grade_blockers
from stockbrain.market_data.sessions import (
    SessionVerdict,
    session_from_schedule,
    session_from_us_clock,
)
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import QuoteSnapshot
from stockbrain.risk.spread import assess_spread

__all__ = ["QuoteFetcher", "session_verdict_for"]


class QuoteFetcher:
    """One fresh, fully-judged quote per call."""

    def __init__(self, provider: MarketDataProvider | None) -> None:
        self._provider = provider

    async def fetch(
        self,
        session: AsyncSession,
        instrument: BrokerInstrument,
        config: RiskConfig,
        *,
        now: dt.datetime | None = None,
    ) -> tuple[QuoteSnapshot | None, str | None]:
        """Return ``(snapshot, None)`` or ``(None, reason)``.

        The reason is what the ``quote_available`` risk rule reports, so a
        blocked proposal explains itself without an operator reading logs.
        """
        moment = now or utcnow()
        if self._provider is None:
            return None, "no market-data provider is configured"
        if not instrument.market_symbol:
            return None, "the listing has no market-data symbol to price"

        try:
            capability = await self._provider.capability()
        except ProviderError as exc:
            return None, f"market-data capability could not be established ({type(exc).__name__})"

        try:
            quote = await self._provider.latest_quote(instrument.market_symbol)
        except ProviderError as exc:
            return None, f"market-data provider failed ({type(exc).__name__})"

        # Source grade only: entitlement, provider health and feed provenance.
        # Age and width are each reported by their own rule with their own
        # numbers -- `quote_freshness` and `spread_ceiling` -- so folding them
        # in here would duplicate the sentence *and*, worse, make a stale quote
        # block `price_source_execution_grade`, which the send-time preflight
        # classifies as a statement about the trade rather than a deferral. A
        # market-data provider running a minute behind would then retire every
        # authorized proposal it touched. (Found in Phase 9; Phase 4's bug 9 and
        # Phase 8's bug 21 are the same distinction at other layers.)
        blockers = tuple(provider_grade_blockers(quote, capability))
        verdict = await session_verdict_for(session, instrument, moment)
        return (
            QuoteSnapshot(
                symbol=quote.symbol,
                provider=quote.provider,
                feed=quote.feed,
                price_source=quote.price_source,
                bid=quote.bid,
                ask=quote.ask,
                mid=quote.price,
                provider_timestamp=quote.provider_timestamp,
                received_at=quote.received_at,
                age_ms=quote.age_ms,
                currency=quote.currency,
                spread=assess_spread(quote.bid, quote.ask, max_spread_bps=config.max_spread_bps),
                session=verdict.session if verdict else _unknown().session,
                session_source=verdict.source if verdict else _unknown().source,
                session_holiday_aware=bool(verdict and verdict.holiday_aware),
                provider_blockers=blockers,
            ),
            None,
        )


def _unknown() -> SessionVerdict:
    from stockbrain.enums import MarketSession

    return SessionVerdict(MarketSession.UNKNOWN, "none")


async def session_verdict_for(
    session: AsyncSession, instrument: BrokerInstrument, moment: dt.datetime
) -> SessionVerdict | None:
    """Prefer the broker's own holiday-aware schedule; fall back to the clock.

    The fallback is only offered to US listings and it says so in its ``source``,
    so a session label never silently claims an authority it does not have.
    """
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
