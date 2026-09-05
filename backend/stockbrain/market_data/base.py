"""The stable internal market-data interface.

Everything downstream -- the price-reaction calculation now, the risk engine in
a later phase -- depends on this module and never on a provider.  Swapping
Alpaca for another vendor must not reach the sizing code, which is why the
protocol is narrow and the DTOs carry provenance rather than just numbers.

Two rules are enforced by the shape of the types:

* **Every financial value is a :class:`~decimal.Decimal`.** There is no float
  anywhere in a quote, a trade or a bar.
* **Every price states where it came from and how old it is.** A price without
  a source and an age cannot be judged, and a price that cannot be judged must
  never size an order.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from stockbrain.enums import (
    EXECUTION_GRADE_PRICE_SOURCES,
    BarTimeframe,
    CapabilityState,
    PriceSource,
)

__all__ = [
    "Bar",
    "MarketDataProvider",
    "ProviderCapability",
    "Quote",
    "Trade",
    "provider_grade_blockers",
    "quote_blockers",
    "to_decimal",
]


def to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    """Convert a JSON number to ``Decimal`` without a binary-float detour.

    ``str(float)`` produces the shortest decimal string that round-trips to the
    same double, which for a value parsed from a JSON literal is that literal.
    ``Decimal(0.1)`` would instead persist
    ``0.1000000000000000055511151231257827``, and a price is not a place to
    discover that binary floating point is inexact.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(slots=True, frozen=True)
class Quote:
    """The best bid and ask for one symbol at one instant."""

    symbol: str
    provider: str
    feed: str
    price_source: PriceSource
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    currency: str = "USD"
    tape: str | None = None
    conditions: tuple[str, ...] = ()
    entitlement: str | None = None
    """Free text recording which plan the feed was served under, when known."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def price(self) -> Decimal | None:
        """Mid price when both sides are live, otherwise the side that exists.

        Alpaca documents ``0`` as "no active bid/ask" rather than a price of
        zero, so a zero side is treated as absent.  A one-sided quote is still
        reported -- with its ``price`` -- but :func:`quote_blockers` refuses to
        let it size anything.
        """
        bid = self.bid if self.bid and self.bid > 0 else None
        ask = self.ask if self.ask and self.ask > 0 else None
        if bid is not None and ask is not None:
            return (bid + ask) / Decimal(2)
        return bid or ask

    @property
    def is_two_sided(self) -> bool:
        return bool(self.bid and self.bid > 0 and self.ask and self.ask > 0)

    @property
    def age_ms(self) -> int:
        """Milliseconds between the provider's timestamp and our receipt.

        Clamped at zero: a provider clock slightly ahead of ours is not evidence
        of a quote from the future.
        """
        delta = (self.received_at - self.provider_timestamp).total_seconds() * 1000.0
        return max(0, int(delta))

    @property
    def spread(self) -> Decimal | None:
        if not self.is_two_sided:
            return None
        assert self.ask is not None and self.bid is not None
        return self.ask - self.bid


@dataclass(slots=True, frozen=True)
class Trade:
    """The most recent trade print for one symbol."""

    symbol: str
    provider: str
    feed: str
    price_source: PriceSource
    price: Decimal
    size: int | None
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    exchange: str | None = None
    tape: str | None = None
    conditions: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def age_ms(self) -> int:
        delta = (self.received_at - self.provider_timestamp).total_seconds() * 1000.0
        return max(0, int(delta))


@dataclass(slots=True, frozen=True)
class Bar:
    """One OHLCV aggregate."""

    symbol: str
    timestamp: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int | None = None
    vwap: Decimal | None = None
    feed: str | None = None


@dataclass(slots=True)
class ProviderCapability:
    """What an actively probed provider can actually reach right now.

    Recorded rather than assumed.  Alpaca's plans differ by *feed*, and a
    configuration that names a feed the account does not own fails at request
    time with an HTTP 403 that looks exactly like a bad credential -- so the
    probe, and this record, are how the two are told apart.
    """

    provider: str
    state: CapabilityState = CapabilityState.UNKNOWN
    feed: str | None = None
    detail: str | None = None
    checked_at: dt.datetime | None = None
    realtime_pricing_usable: bool = False
    """True only when a usable, execution-grade, two-sided quote was actually
    returned.  Nothing downstream may size a position when this is False."""

    probe_symbol: str | None = None
    probe_quote_age_ms: int | None = None
    probe_quote_stale: bool = False
    """The probe quote was older than the configured maximum age.

    Separate from :attr:`state` on purpose.  Entitlement and freshness are
    different facts: outside market hours *every* quote is the closing print, so
    reporting DEGRADED overnight would be noise rather than signal.  The feed is
    entitled and answering (``state`` HEALTHY, ``realtime_pricing_usable`` True)
    while sizing right now is still refused by :func:`quote_blockers` on age.
    A live probe at 03:40 UTC returned a 7.7-hour-old quote and reported itself
    usable, which was accurate and unreadable at the same time; this flag is
    what makes the two statements legible together."""

    available_feeds: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "state": self.state.value,
            "feed": self.feed,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "realtime_pricing_usable": self.realtime_pricing_usable,
            "probe_symbol": self.probe_symbol,
            "probe_quote_age_ms": self.probe_quote_age_ms,
            "probe_quote_stale": self.probe_quote_stale,
            "available_feeds": list(self.available_feeds),
            "blockers": list(self.blockers),
        }


def provider_grade_blockers(
    quote: Quote | None, capability: ProviderCapability | None
) -> list[str]:
    """Reasons this *source* may not price an order, independent of this moment.

    Entitlement and provenance only: is a capability established, is the
    provider healthy, does the plan include usable real-time pricing, and is
    the feed one of the execution-grade sources.

    Deliberately **not** freshness and **not** width.  Phase 4's bug 9 was the
    capability probe conflating entitlement with freshness, and this is the same
    distinction one layer up: ``quote_freshness`` owns the age and
    ``spread_ceiling`` owns the width, each with its own numbers and its own
    threshold.  Keeping them apart matters beyond tidiness -- Phase 8's
    ``preflight`` classifies a *stale* input as a deferral and everything else
    as a statement about the trade, so a stale quote leaking into a
    source-grade blocker retires an authorized proposal that a provider running
    a minute behind should merely have delayed.  (Found in Phase 9.)
    """
    blockers: list[str] = []
    if capability is None:
        blockers.append("no market-data provider capability has been established")
    elif capability.state is not CapabilityState.HEALTHY:
        blockers.append(f"market-data provider is {capability.state.value}")
    elif not capability.realtime_pricing_usable:
        blockers.append("provider has no usable real-time pricing")

    if quote is None:
        blockers.append("no quote is available for this instrument")
        return blockers

    if quote.price_source not in EXECUTION_GRADE_PRICE_SOURCES:
        blockers.append(f"price source {quote.price_source.value} is display/reconciliation only")
    return blockers


def quote_blockers(
    quote: Quote | None,
    capability: ProviderCapability | None,
    *,
    max_age_seconds: float,
    max_spread_bps: Decimal | None = None,
) -> list[str]:
    """Every reason this quote may not be used to size a real order.

    An empty list is the *only* thing that permits sizing.  This is the single
    predicate every sizing path consults, so a new condition is added *here*
    rather than rewritten at each call site.

    Trading 212's own price data can never clear this bar: its API Terms do not
    guarantee it is real-time, so ``PriceSource.BROKER_T212`` is not in
    ``EXECUTION_GRADE_PRICE_SOURCES`` and never becomes execution pricing by
    being the only number available.

    ``max_spread_bps`` adds the Phase 6 relative-width ceiling.  It is optional
    only so that inspection endpoints can ask "is this quote fresh and
    execution-grade?" without also asserting a sizing policy; every path that
    actually sizes something passes it.  Age and width are independent: the
    live overnight AAPL book was both stale *and* 1024 bps wide, and a book that
    wide during regular hours would be fresh and still unusable.
    """
    blockers = provider_grade_blockers(quote, capability)
    if quote is None:
        return blockers

    if not quote.is_two_sided:
        blockers.append("quote is not two-sided (no live bid or no live ask)")
    if quote.price is None or quote.price <= 0:
        blockers.append("quote carries no positive price")
    if quote.age_ms > max_age_seconds * 1000:
        blockers.append(f"quote is {quote.age_ms}ms old, older than the {max_age_seconds:g}s limit")
    if max_spread_bps is not None:
        # Imported here rather than at module scope: `risk` depends on
        # `market_data`, and the DTO layer must not acquire a dependency on the
        # policy layer that reads it.
        from stockbrain.risk.spread import assess_spread

        assessment = assess_spread(quote.bid, quote.ask, max_spread_bps=max_spread_bps)
        if not assessment.is_ok:
            blockers.append(assessment.detail)
    return blockers


@runtime_checkable
class MarketDataProvider(Protocol):
    """The only market-data surface the rest of StockBrain may depend on."""

    name: str

    async def latest_quote(self, symbol: str) -> Quote:
        """Best bid/ask for ``symbol``.  Raises a ``ProviderError`` subclass."""
        ...

    async def latest_trade(self, symbol: str) -> Trade:
        """Most recent trade print for ``symbol``."""
        ...

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> Sequence[Bar]:
        """Historical aggregates in ``[start, end]``, oldest first."""
        ...

    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        """What this provider can currently reach, probed rather than assumed."""
        ...
