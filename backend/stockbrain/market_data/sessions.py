"""Which trading session an instant falls in.

Two sources, in order of authority:

1. **The broker's own working schedule.** Trading 212's
   ``/equity/metadata/exchanges`` returns each exchange's schedule as dated
   ``timeEvents`` -- ``PRE_MARKET_OPEN``, ``OPEN``, ``CLOSE``,
   ``AFTER_HOURS_CLOSE`` and so on.  That data is holiday-aware, because it is
   the actual calendar the broker will trade against.
2. **A US-equity clock fallback**, used only when no schedule covers the
   instant.  It is explicitly *not* holiday-aware, and it says so: the result
   carries which source produced it, so a caller can tell a real session from a
   clock guess.

Anything else returns ``UNKNOWN``.  A wrong session label on an event is worse
than no label: "the stock did not move because the market was shut" and "the
stock did not move despite the market being open" are opposite conclusions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from stockbrain.enums import MarketSession

__all__ = [
    "SessionVerdict",
    "session_from_schedule",
    "session_from_us_clock",
]

_NEW_YORK = ZoneInfo("America/New_York")

#: US equity session boundaries in exchange-local time (spec section 12's
#: pre/regular/after split).  Used only by the clock fallback.
_PRE_MARKET_OPEN = dt.time(4, 0)
_REGULAR_OPEN = dt.time(9, 30)
_REGULAR_CLOSE = dt.time(16, 0)
_AFTER_HOURS_CLOSE = dt.time(20, 0)

#: How each Trading 212 time-event type opens or closes a session.  Unknown
#: event types are ignored rather than guessed at.
_OPENS: dict[str, MarketSession] = {
    "PRE_MARKET_OPEN": MarketSession.PRE_MARKET,
    "OPEN": MarketSession.REGULAR,
    "BREAK_END": MarketSession.REGULAR,
    "AFTER_HOURS_OPEN": MarketSession.AFTER_HOURS,
    "OVERNIGHT_OPEN": MarketSession.OVERNIGHT,
}
_CLOSES = frozenset({"CLOSE", "BREAK_START", "AFTER_HOURS_CLOSE"})


@dataclass(slots=True, frozen=True)
class SessionVerdict:
    session: MarketSession
    source: str
    """``exchange_schedule``, ``us_clock`` or ``none`` -- so a caller can weigh it."""

    holiday_aware: bool = False


def session_from_schedule(
    time_events: list[dict[str, object]], instant: dt.datetime
) -> SessionVerdict:
    """Classify ``instant`` against a broker working schedule.

    The schedule is a sequence of dated boundaries; the session is whatever the
    most recent boundary at or before the instant opened.  If the instant is
    before every boundary, or the schedule is empty, the answer is UNKNOWN --
    the schedule only covers a rolling window, and extrapolating past its edge
    would be a guess wearing the schedule's authority.
    """
    parsed: list[tuple[dt.datetime, str]] = []
    for event in time_events:
        raw_date = event.get("date")
        raw_type = event.get("type")
        if not isinstance(raw_date, str) or not isinstance(raw_type, str):
            continue
        try:
            moment = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        parsed.append((moment.astimezone(dt.UTC), raw_type.upper()))

    if not parsed:
        return SessionVerdict(MarketSession.UNKNOWN, "none")

    parsed.sort(key=lambda item: item[0])
    target = instant.astimezone(dt.UTC)
    if target < parsed[0][0]:
        return SessionVerdict(MarketSession.UNKNOWN, "none")

    current = MarketSession.CLOSED
    seen_known = False
    for moment, kind in parsed:
        if moment > target:
            break
        if kind in _OPENS:
            current = _OPENS[kind]
            seen_known = True
        elif kind in _CLOSES:
            current = MarketSession.CLOSED
            seen_known = True

    if not seen_known:
        return SessionVerdict(MarketSession.UNKNOWN, "none")
    return SessionVerdict(current, "exchange_schedule", holiday_aware=True)


def session_from_us_clock(instant: dt.datetime) -> SessionVerdict:
    """Fallback classification for a US listing, from the exchange clock alone.

    **Not holiday-aware.**  On Thanksgiving this says REGULAR, which is why the
    verdict carries ``holiday_aware=False`` and callers report the source.
    """
    local = instant.astimezone(_NEW_YORK)
    if local.weekday() >= 5:
        return SessionVerdict(MarketSession.CLOSED, "us_clock")

    clock = local.time()
    if clock < _PRE_MARKET_OPEN or clock >= _AFTER_HOURS_CLOSE:
        return SessionVerdict(MarketSession.CLOSED, "us_clock")
    if clock < _REGULAR_OPEN:
        return SessionVerdict(MarketSession.PRE_MARKET, "us_clock")
    if clock < _REGULAR_CLOSE:
        return SessionVerdict(MarketSession.REGULAR, "us_clock")
    return SessionVerdict(MarketSession.AFTER_HOURS, "us_clock")
