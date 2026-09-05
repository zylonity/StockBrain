"""Typed foreign-exchange rates, and the rules for trusting one.

Phase 6 measured the live Trading 212 account as **GBP** with **14 of 14**
positions denominated in another currency, so ``currency_alignment`` blocked
every proposal the system could price.  The refusal was correct: a cap
denominated in one currency cannot bound a price denominated in another, and an
invented rate is a wrong size wearing a plausible number.

This module is what makes cross-currency sizing possible *without* inventing
anything.  Four ideas carry the safety:

**A rate is a measurement, not a number.**  :class:`FxRate` carries its base and
quote currencies, its provider, the provider's own timestamp, the instant it was
received and therefore its age.  A bare ``Decimal("1.35")`` cannot be checked
for freshness, attributed to a source or audited after the fact, so no function
here accepts or returns one.

**Direction is explicit.**  ``rate`` always means *units of ``quote`` per one
unit of ``base``*.  A GBP/USD rate of 1.35 means one pound buys 1.35 dollars.
:meth:`FxRate.convert` will invert that relationship arithmetically -- ``1 /
rate`` is exact given the pair, not a second opinion about the market -- and
will refuse any pair it was not measured for.

**There is no multi-hop.**  If the rate on hand is GBP/USD and the question is
EUR/JPY, the answer is a refusal.  Chaining two published rates produces a third
number that no source published and no counterparty will honour, and the error
compounds silently.

**Not every source is execution-grade.**  A daily central-bank reference fixing
and a live dealable quote are both "the exchange rate" and they are not
interchangeable.  :class:`FxRateGrade` makes the difference a property of the
provider, and a reference-grade rate may only size a trade when the operator has
said so explicitly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FxCapability",
    "FxConversion",
    "FxDirection",
    "FxPairMismatchError",
    "FxRate",
    "FxRateGrade",
    "FxRateProvider",
    "currency_pair",
    "fx_blockers",
    "identity_rate",
    "normalize_currency",
]


class FxRateGrade(StrEnum):
    """How close a source is to a price somebody would actually deal on.

    ``EXECUTION`` is a live two-sided or mid market quote with a sub-minute
    timestamp -- Alpaca's forex feed, for example.  ``REFERENCE`` is a published
    fixing: authoritative, daily, and explicitly not a dealing rate.  The
    Frankfurter service (ECB and other central-bank reference rates) says so in
    its own documentation: "not for live trading".

    StockBrain does not treat that as a reason to reject reference rates
    outright -- sizing a small equity position against a fixing that is hours
    old is defensible in a way that *executing* against it is not -- but it does
    treat it as a reason the operator has to opt in, and as a different
    freshness budget.
    """

    EXECUTION = "EXECUTION"
    REFERENCE = "REFERENCE"


class FxDirection(StrEnum):
    """Which way a conversion used the rate it was given."""

    DIRECT = "DIRECT"
    """``amount * rate``: the requested pair matched the measured pair."""

    INVERTED = "INVERTED"
    """``amount / rate``: the measured pair was the other way round."""

    IDENTITY = "IDENTITY"
    """Same currency both sides.  Not a rate at all, and never a stand-in for
    one: :func:`identity_rate` is only ever constructed when the two currency
    codes are equal, so "silently used 1.0 for a cross-currency trade" is not a
    state this type can represent."""


class FxPairMismatchError(ValueError):
    """The rate on hand does not measure the pair that was asked about."""


def normalize_currency(code: str | None) -> str:
    """Upper-case, whitespace-stripped ISO 4217 code, or ``""``.

    Currency codes arrive from three providers with three conventions; comparing
    them without normalising is how ``"usd" != "USD"`` becomes a blocked trade
    nobody can explain.
    """
    return (code or "").strip().upper()


def currency_pair(base: str, quote: str) -> str:
    """The conventional concatenated pair symbol, e.g. ``GBPUSD``."""
    return f"{normalize_currency(base)}{normalize_currency(quote)}"


@dataclass(frozen=True, slots=True)
class FxConversion:
    """One conversion, with everything needed to re-derive it later.

    Persisted onto a proposal.  A converted number without its rate, its
    direction and the age of that rate is a number nobody can check.
    """

    amount: Decimal
    from_currency: str
    to_currency: str
    converted: Decimal
    rate: Decimal
    direction: FxDirection
    provider: str
    base_currency: str
    quote_currency: str
    grade: FxRateGrade
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    age_seconds: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": str(self.amount),
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "converted": str(self.converted),
            "rate": str(self.rate),
            "direction": self.direction.value,
            "provider": self.provider,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "pair": currency_pair(self.base_currency, self.quote_currency),
            "grade": self.grade.value,
            "provider_timestamp": self.provider_timestamp.isoformat(),
            "received_at": self.received_at.isoformat(),
            "age_seconds": str(self.age_seconds),
        }


@dataclass(frozen=True, slots=True)
class FxRate:
    """One measured exchange rate.

    ``rate`` is units of :attr:`quote_currency` per **one** unit of
    :attr:`base_currency`.
    """

    base_currency: str
    quote_currency: str
    rate: Decimal
    provider: str
    grade: FxRateGrade
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    rate_type: str = "mid"
    """What the number is: ``mid``, ``bid``, ``ask``, or a provider's own term
    such as ``ecb_reference``.  Recorded rather than assumed, because a mid and
    a reference fixing are different measurements of the same pair."""

    bid: Decimal | None = None
    ask: Decimal | None = None
    provider_timestamp_precision: str = "instant"
    """``instant`` when the provider timestamped the measurement itself, ``day``
    when it published only a date.  A date-only publication is floored to the
    start of that day in UTC, which over-states the age -- the safe direction
    for a freshness check."""

    def __post_init__(self) -> None:
        # Constructed by adapters from provider payloads, so the invariants are
        # checked here rather than trusted. A non-positive or reversed rate is a
        # parsing failure, and a parsing failure that produces a plausible
        # Decimal is the most expensive kind.
        if self.rate <= 0:
            raise ValueError(
                f"an FX rate must be positive (got {self.rate} for "
                f"{self.base_currency}/{self.quote_currency})"
            )
        if normalize_currency(self.base_currency) == normalize_currency(self.quote_currency):
            raise ValueError(
                "a measured FX rate must have two different currencies; use "
                "identity_rate() for a same-currency conversion"
            )

    @property
    def pair(self) -> str:
        return currency_pair(self.base_currency, self.quote_currency)

    def age_seconds(self, now: dt.datetime) -> Decimal:
        """Age against ``now``, clamped at zero.

        A provider clock slightly ahead of ours is not a rate from the future,
        and a negative age would sail through every freshness check.
        """
        delta = (now - self.provider_timestamp).total_seconds()
        return Decimal(str(max(0.0, delta)))

    def supports(self, from_currency: str, to_currency: str) -> bool:
        """Whether this measurement covers the requested direction."""
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)
        return (source, target) in {(base, quote), (quote, base)}

    def convert(
        self, amount: Decimal, from_currency: str, to_currency: str, *, now: dt.datetime
    ) -> FxConversion:
        """Convert ``amount``, or raise :class:`FxPairMismatchError`.

        Inversion is arithmetic on the pair that *was* measured, which is why it
        is allowed: ``USD -> GBP`` from a GBP/USD rate is ``amount / rate`` and
        introduces no source and no assumption.  Anything else -- a third
        currency, a chain through a pivot -- raises.
        """
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)

        if source == target:  # pragma: no cover - callers use identity_rate
            raise FxPairMismatchError(
                "a same-currency conversion must not consult a measured rate; use identity_rate()"
            )
        if (source, target) == (base, quote):
            converted, direction = amount * self.rate, FxDirection.DIRECT
        elif (source, target) == (quote, base):
            converted, direction = amount / self.rate, FxDirection.INVERTED
        else:
            raise FxPairMismatchError(
                f"the {self.pair} rate from {self.provider} cannot convert "
                f"{source} to {target}: StockBrain never chains rates through a "
                f"third currency"
            )
        return FxConversion(
            amount=amount,
            from_currency=source,
            to_currency=target,
            converted=converted,
            rate=self.rate,
            direction=direction,
            provider=self.provider,
            base_currency=base,
            quote_currency=quote,
            grade=self.grade,
            provider_timestamp=self.provider_timestamp,
            received_at=self.received_at,
            age_seconds=self.age_seconds(now),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_currency": normalize_currency(self.base_currency),
            "quote_currency": normalize_currency(self.quote_currency),
            "pair": self.pair,
            "rate": str(self.rate),
            "rate_type": self.rate_type,
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "provider": self.provider,
            "grade": self.grade.value,
            "provider_timestamp": self.provider_timestamp.isoformat(),
            "provider_timestamp_precision": self.provider_timestamp_precision,
            "received_at": self.received_at.isoformat(),
        }


def identity_rate(currency: str, *, now: dt.datetime) -> FxConversion:
    """The conversion for "these are the same currency".

    Returned as a full :class:`FxConversion` with ``IDENTITY`` direction and a
    zero age, so a same-currency proposal records *why* no rate was needed
    rather than recording nothing and leaving a reader to infer it.  This is the
    only place the number one appears in an FX path, and it appears only when
    the two codes are equal.
    """
    code = normalize_currency(currency)
    return FxConversion(
        amount=Decimal(0),
        from_currency=code,
        to_currency=code,
        converted=Decimal(0),
        rate=Decimal(1),
        direction=FxDirection.IDENTITY,
        provider="identity",
        base_currency=code,
        quote_currency=code,
        grade=FxRateGrade.EXECUTION,
        provider_timestamp=now,
        received_at=now,
        age_seconds=Decimal(0),
    )


def fx_blockers(
    rate: FxRate | None,
    *,
    from_currency: str,
    to_currency: str,
    now: dt.datetime,
    max_age_seconds: Decimal,
    reference_max_age_seconds: Decimal,
    allow_reference_grade: bool,
    missing_reason: str | None = None,
) -> list[str]:
    """Every reason this rate may not size a trade, in GUI-ready wording.

    The single predicate for FX, mirroring
    :func:`stockbrain.market_data.base.quote_blockers`: one function that every
    caller consults, so the proposal view, the risk rule and the send-time
    recheck cannot disagree about whether a rate is usable.

    An empty list means the rate may be used.  A same-currency request needs no
    rate and returns an empty list with ``rate`` of ``None`` -- that is the only
    case in which a missing rate is not a blocker.
    """
    source = normalize_currency(from_currency)
    target = normalize_currency(to_currency)
    if not source or not target:
        return ["the instrument or account currency is unknown, so no conversion is defined"]
    if source == target:
        return []

    if rate is None:
        return [
            missing_reason
            or (
                f"no FX rate is available for {source}->{target}; cross-currency "
                f"sizing is blocked rather than assuming a rate"
            )
        ]

    blockers: list[str] = []
    if not rate.supports(source, target):
        blockers.append(
            f"the available rate measures {rate.pair}, which cannot convert "
            f"{source} to {target} without chaining through a third currency"
        )
        # Every remaining check would be about the wrong pair.
        return blockers

    if rate.grade is FxRateGrade.REFERENCE and not allow_reference_grade:
        blockers.append(
            f"the {rate.provider} rate is a {rate.grade.value.lower()}-grade fixing, not a "
            f"dealable quote; set FX_ALLOW_REFERENCE_GRADE=true to size against it knowingly"
        )

    limit = reference_max_age_seconds if rate.grade is FxRateGrade.REFERENCE else max_age_seconds
    age = rate.age_seconds(now)
    if age > limit:
        blockers.append(
            f"the {rate.pair} rate is {age}s old, older than the {limit}s limit for a "
            f"{rate.grade.value.lower()}-grade source"
        )
    return blockers


@dataclass(frozen=True, slots=True)
class FxCapability:
    """What an FX provider can currently do, and why not if it cannot.

    Same shape as the market-data capability probe: ``available`` is defined as
    "no blockers remain", so a health panel cannot show a blocker beside a green
    light.
    """

    provider: str
    grade: FxRateGrade | None
    """``None`` only when no provider is configured.  A placeholder grade would
    read as "a source of that kind is available" in a health panel."""

    blockers: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def available(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "grade": self.grade.value if self.grade else None,
            "available": self.available,
            "blockers": list(self.blockers),
            "detail": self.detail,
        }


@runtime_checkable
class FxRateProvider(Protocol):
    """One replaceable source of exchange rates.

    Narrow on purpose: a rate for one ordered pair, and a statement about
    whether the source can be used at all.  No history, no conversion helpers,
    no caching -- conversion belongs to :class:`FxRate`, which carries the
    provenance, and caching an execution-grade rate is how a stale one gets
    used.
    """

    name: str
    grade: FxRateGrade

    async def latest(self, base: str, quote: str) -> FxRate:
        """Fetch the current rate for ``base``/``quote``.

        Implementations may return the pair measured in the other direction --
        market convention varies, and inverting is exact -- but must never
        return a pair involving a currency that was not asked for.
        """
        ...

    async def capability(self) -> FxCapability:
        """Whether this source is usable, measured rather than assumed."""
        ...

    async def aclose(self) -> None: ...
