"""How much has a security moved since an event became public?

Phase 4 answers that question and stops there.  The number is *context* shown
next to an event, not a signal: nothing sizes, ranks or decides on it, and the
risk engine that eventually will is a later phase's problem.

The measurement is deliberately explicit about its own weaknesses:

* the price "at the event" is the open of the first bar at or after the event
  timestamp -- if none exists (the event is minutes old, or the market was shut)
  the result says so rather than reaching backwards and pretending;
* a fallback to the last bar *before* the event is offered, but labelled, since
  "the last trade before the news" is a different measurement;
* the reference price prefers a live quote and falls back to the most recent
  bar close, recording which it used and how old it was;
* the session the event landed in is reported with its own provenance, because
  a clock-derived session is not holiday-aware.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.db.base import utcnow
from stockbrain.enums import BarTimeframe, MarketSession, ReactionStatus
from stockbrain.errors import ProviderError
from stockbrain.logging import get_logger
from stockbrain.market_data.base import Bar, MarketDataProvider, Quote
from stockbrain.market_data.sessions import SessionVerdict, session_from_us_clock

__all__ = ["PriceReaction", "PriceReactionCalculator"]

log = get_logger(__name__)

#: How far before the event to fetch bars, so a fallback "last price before the
#: news" exists even when the event landed in a quiet minute.
_LOOKBACK = dt.timedelta(minutes=30)

#: Cap on the measured window. Beyond this the question stops being "how did it
#: react" and starts being "how has it drifted", which is a different question.
MAX_WINDOW = dt.timedelta(days=5)


@dataclass(slots=True)
class PriceReaction:
    """The move since an event, with everything needed to distrust it."""

    symbol: str
    event_time: dt.datetime
    status: ReactionStatus = ReactionStatus.NO_DATA
    provider: str | None = None
    feed: str | None = None

    price_at_event: Decimal | None = None
    price_at_event_time: dt.datetime | None = None
    price_at_event_basis: str | None = None
    """``first_bar_at_or_after_event`` or ``last_bar_before_event``."""

    reference_price: Decimal | None = None
    reference_price_time: dt.datetime | None = None
    reference_price_basis: str | None = None
    """``quote_mid`` or ``last_bar_close``."""

    reference_quote_age_ms: int | None = None
    absolute_move: Decimal | None = None
    percent_move: Decimal | None = None
    elapsed_seconds: float = 0.0
    session_at_event: MarketSession = MarketSession.UNKNOWN
    session_source: str = "none"
    session_holiday_aware: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "event_time": self.event_time.isoformat(),
            "status": self.status.value,
            "provider": self.provider,
            "feed": self.feed,
            "price_at_event": str(self.price_at_event) if self.price_at_event else None,
            "price_at_event_time": (
                self.price_at_event_time.isoformat() if self.price_at_event_time else None
            ),
            "price_at_event_basis": self.price_at_event_basis,
            "reference_price": str(self.reference_price) if self.reference_price else None,
            "reference_price_time": (
                self.reference_price_time.isoformat() if self.reference_price_time else None
            ),
            "reference_price_basis": self.reference_price_basis,
            "reference_quote_age_ms": self.reference_quote_age_ms,
            "absolute_move": str(self.absolute_move) if self.absolute_move is not None else None,
            "percent_move": str(self.percent_move) if self.percent_move is not None else None,
            "elapsed_seconds": self.elapsed_seconds,
            "session_at_event": self.session_at_event.value,
            "session_source": self.session_source,
            "session_holiday_aware": self.session_holiday_aware,
            "notes": list(self.notes),
        }


class PriceReactionCalculator:
    """Computes the post-event move for one symbol."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    async def compute(
        self,
        symbol: str,
        event_time: dt.datetime,
        *,
        now: dt.datetime | None = None,
        session: SessionVerdict | None = None,
    ) -> PriceReaction:
        """Measure the move between the event and the current reference price."""
        now = now or utcnow()
        reaction = PriceReaction(symbol=symbol, event_time=event_time, provider=self._provider.name)

        verdict = session or session_from_us_clock(event_time)
        reaction.session_at_event = verdict.session
        reaction.session_source = verdict.source
        reaction.session_holiday_aware = verdict.holiday_aware

        if event_time > now:
            reaction.status = ReactionStatus.NO_DATA
            reaction.notes.append("event timestamp is in the future")
            return reaction

        window_start = event_time - _LOOKBACK
        window_end = now
        if window_end - event_time > MAX_WINDOW:
            window_end = event_time + MAX_WINDOW
            reaction.notes.append(f"window truncated to {MAX_WINDOW.days}d after the event")

        try:
            bars = list(
                await self._provider.bars(
                    symbol, BarTimeframe.MIN_1, window_start, window_end, limit=2000
                )
            )
        except ProviderError as exc:
            reaction.status = ReactionStatus.PROVIDER_UNAVAILABLE
            reaction.notes.append(f"{type(exc).__name__}: {exc}"[:200])
            return reaction

        if bars:
            reaction.feed = bars[0].feed

        at_event = _first_at_or_after(bars, event_time)
        if at_event is not None:
            reaction.price_at_event = at_event.open
            reaction.price_at_event_time = at_event.timestamp
            reaction.price_at_event_basis = "first_bar_at_or_after_event"
        else:
            before = _last_before(bars, event_time)
            if before is not None:
                reaction.price_at_event = before.close
                reaction.price_at_event_time = before.timestamp
                reaction.price_at_event_basis = "last_bar_before_event"
                reaction.notes.append(
                    "no bar exists at or after the event; using the last price before it, "
                    "which measures a different thing and must not be read as a reaction"
                )

        quote = await self._reference_quote(symbol, reaction)
        if quote is not None and quote.price is not None:
            reaction.reference_price = quote.price
            reaction.reference_price_time = quote.provider_timestamp
            reaction.reference_price_basis = "quote_mid"
            reaction.reference_quote_age_ms = quote.age_ms
            reaction.feed = quote.feed
        elif bars:
            reaction.reference_price = bars[-1].close
            reaction.reference_price_time = bars[-1].timestamp
            reaction.reference_price_basis = "last_bar_close"

        return _finalise(reaction, now)

    async def _reference_quote(self, symbol: str, reaction: PriceReaction) -> Quote | None:
        try:
            return await self._provider.latest_quote(symbol)
        except ProviderError as exc:
            # A missing quote is not a failure of the whole measurement: the
            # last bar close still answers the question, less precisely.
            reaction.notes.append(f"no live quote ({type(exc).__name__})")
            return None


def _finalise(reaction: PriceReaction, now: dt.datetime) -> PriceReaction:
    reaction.elapsed_seconds = max(0.0, (now - reaction.event_time).total_seconds())

    if reaction.price_at_event is None and reaction.reference_price is None:
        reaction.status = ReactionStatus.NO_DATA
        return reaction
    if reaction.price_at_event is None:
        reaction.status = ReactionStatus.NO_PRICE_AT_EVENT
        return reaction
    if reaction.reference_price is None:
        reaction.status = ReactionStatus.NO_CURRENT_PRICE
        return reaction

    reaction.absolute_move = reaction.reference_price - reaction.price_at_event
    if reaction.price_at_event > 0:
        reaction.percent_move = (
            reaction.absolute_move / reaction.price_at_event * Decimal(100)
        ).quantize(Decimal("0.0001"))
    reaction.status = ReactionStatus.OK
    return reaction


def _first_at_or_after(bars: list[Bar], moment: dt.datetime) -> Bar | None:
    for bar in bars:
        if bar.timestamp >= moment:
            return bar
    return None


def _last_before(bars: list[Bar], moment: dt.datetime) -> Bar | None:
    candidate: Bar | None = None
    for bar in bars:
        if bar.timestamp < moment:
            candidate = bar
        else:
            break
    return candidate
