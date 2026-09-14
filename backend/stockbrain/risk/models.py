"""Typed inputs and outputs of the deterministic risk engine.

Everything the engine reads is in :class:`RiskInputs`, and everything it
concluded is in :class:`RiskDecision`.  The engine itself is a pure function of
one to the other: no database, no HTTP, no clock of its own.  That is what makes
"blocked risk never reaches the broker" testable rather than hopeful.

Two shapes deserve their reasons stated:

* :class:`RuleResult` is structured -- rule id, rule version, outcome, the value
  observed, the threshold it was compared against, and a sentence.  A risk
  refusal recorded as free text is a refusal nobody can audit or aggregate.
* :class:`AccountState` and :class:`InstrumentIdentity` are snapshots taken at a
  named instant, not live handles.  The engine must judge the same numbers that
  get persisted onto the proposal, or the record and the decision diverge.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.enums import (
    Broker,
    MarketSession,
    OrderSide,
    PriceSource,
    ResolutionStatus,
    RiskOutcome,
    RuleOutcome,
    ThesisAction,
)
from stockbrain.fx.base import FxDirection, FxRateGrade, currency_pair, normalize_currency
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.spread import SpreadAssessment

__all__ = [
    "ZERO",
    "AccountState",
    "FxSnapshot",
    "InstrumentIdentity",
    "PositionState",
    "QuoteSnapshot",
    "ReservedExposure",
    "RiskDecision",
    "RiskInputs",
    "RuleResult",
    "SizingResult",
]

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class PositionState:
    """One holding as the broker last reported it."""

    broker_ticker: str
    quantity: Decimal
    quantity_available: Decimal
    currency: str | None = None
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    """In the *account's* currency.  Trading 212 reports position wallet impact
    in the primary account currency, so this needs no conversion of ours."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker_ticker": self.broker_ticker,
            "quantity": str(self.quantity),
            "quantity_available": str(self.quantity_available),
            "currency": self.currency,
            "average_price": _opt_str(self.average_price),
            "current_price": _opt_str(self.current_price),
            "market_value": _opt_str(self.market_value),
        }


@dataclass(frozen=True, slots=True)
class AccountState:
    """Broker account state at one instant, read-only.

    ``captured_at`` is the broker read time, not the row's write time: staleness
    is measured from when the numbers were true, not from when we stored them.
    """

    broker: Broker
    account_id: str
    currency: str
    cash_available: Decimal
    total_value: Decimal
    captured_at: dt.datetime
    cash_reserved: Decimal = ZERO
    cash_in_pies: Decimal = ZERO
    invested_value: Decimal = ZERO
    positions: Mapping[str, PositionState] = field(default_factory=dict)

    def age_seconds(self, now: dt.datetime) -> Decimal:
        """Clamped at zero: a broker clock slightly ahead is not a future read."""
        delta = (now - self.captured_at).total_seconds()
        return Decimal(str(max(0.0, delta)))

    def position(self, broker_ticker: str) -> PositionState | None:
        return self.positions.get(broker_ticker)

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker.value,
            "account_id": self.account_id,
            "currency": self.currency,
            "cash_available": str(self.cash_available),
            "cash_reserved": str(self.cash_reserved),
            "cash_in_pies": str(self.cash_in_pies),
            "invested_value": str(self.invested_value),
            "total_value": str(self.total_value),
            "captured_at": self.captured_at.isoformat(),
            "position_count": len(self.positions),
        }


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    """The verified broker listing a proposal is about.

    ``broker_ticker`` is the only identity an order may ever carry, and it is
    only ever copied from a ``broker_instruments`` row the resolver selected.
    ``market_symbol`` is a *lookup key* for the market-data provider and must
    never be sent to a broker.
    """

    broker_instrument_id: Any
    broker: Broker
    broker_ticker: str
    market_symbol: str | None
    resolution_status: ResolutionStatus
    is_active: bool
    instrument_type: str | None = None
    currency: str | None = None
    exchange: str | None = None
    isin: str | None = None
    company_id: Any = None
    max_open_quantity: Decimal | None = None
    extended_hours: bool = False
    quantity_precision: int | None = None
    """Decimal places the broker accepts on an order quantity, learned from a
    ``quantity-precision-mismatch`` refusal.  ``None`` means unknown, and the
    risk config's default stands in until the broker says otherwise.  Appended
    last so positional construction everywhere keeps working."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker_instrument_id": str(self.broker_instrument_id),
            "broker": self.broker.value,
            "broker_ticker": self.broker_ticker,
            "market_symbol": self.market_symbol,
            "resolution_status": self.resolution_status.value,
            "is_active": self.is_active,
            "instrument_type": self.instrument_type,
            "currency": self.currency,
            "exchange": self.exchange,
            "isin": self.isin,
            "company_id": str(self.company_id) if self.company_id else None,
            "max_open_quantity": _opt_str(self.max_open_quantity),
            "extended_hours": self.extended_hours,
            "quantity_precision": self.quantity_precision,
        }


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """The exact price facts a decision was made on.

    A price without a source and an age may not size anything, so all three
    travel together and all three are persisted onto the proposal.
    """

    symbol: str
    provider: str
    feed: str
    price_source: PriceSource
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    age_ms: int
    currency: str
    spread: SpreadAssessment
    session: MarketSession = MarketSession.UNKNOWN
    session_source: str = "none"
    session_holiday_aware: bool = False
    provider_blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "feed": self.feed,
            "price_source": self.price_source.value,
            "bid": _opt_str(self.bid),
            "ask": _opt_str(self.ask),
            "mid": _opt_str(self.mid),
            "provider_timestamp": self.provider_timestamp.isoformat(),
            "received_at": self.received_at.isoformat(),
            "quote_age_ms": self.age_ms,
            "currency": self.currency,
            "spread": self.spread.as_dict(),
            "session": self.session.value,
            "session_source": self.session_source,
            "session_holiday_aware": self.session_holiday_aware,
            "provider_blockers": list(self.provider_blockers),
        }


@dataclass(frozen=True, slots=True)
class FxSnapshot:
    """The FX facts a decision was made on, frozen at one instant.

    A snapshot, not a live handle -- exactly like :class:`QuoteSnapshot`, and
    for the same reason: the engine must judge the numbers that get persisted
    onto the proposal, or the record and the decision diverge.

    Two shapes it can take, and only two:

    * **Same currency.**  ``same_currency`` is true, ``rate`` is ``None`` and
      no conversion happens.  Reported as its own state rather than as a rate
      of one, so a reader can tell "no conversion was needed" from "converted
      at parity".
    * **A measured rate.**  ``rate`` is present with its pair, provider, grade,
      timestamp and age, and ``blockers`` is empty.

    Anything else -- no rate, the wrong pair, a stale rate, a reference-grade
    rate the operator has not permitted -- arrives with ``blockers`` populated,
    ``usable`` false, and every conversion method refusing.  There is no
    representable state in which two different currencies are converted at 1.0.
    """

    account_currency: str
    instrument_currency: str
    same_currency: bool
    blockers: tuple[str, ...] = ()
    rate: Decimal | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    provider: str | None = None
    grade: FxRateGrade | None = None
    rate_type: str | None = None
    provider_timestamp: dt.datetime | None = None
    received_at: dt.datetime | None = None
    age_seconds: Decimal | None = None
    provider_timestamp_precision: str | None = None

    @property
    def usable(self) -> bool:
        """Whether a conversion may be performed at all.

        Defined as "no blockers remain", so this and :attr:`blockers` cannot
        disagree.
        """
        return not self.blockers

    @property
    def pair(self) -> str | None:
        if self.base_currency is None or self.quote_currency is None:
            return None
        return currency_pair(self.base_currency, self.quote_currency)

    @property
    def conversion_required(self) -> bool:
        return not self.same_currency

    def _direction_for(self, *, from_currency: str, to_currency: str) -> FxDirection:
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        if source == target:
            return FxDirection.IDENTITY
        if not self.usable or self.rate is None:
            raise ValueError(
                "this FX snapshot may not convert anything: " + "; ".join(self.blockers)
            )
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)
        if (source, target) == (base, quote):
            return FxDirection.DIRECT
        if (source, target) == (quote, base):
            return FxDirection.INVERTED
        raise ValueError(
            f"the {self.pair} rate cannot convert {source} to {target}; StockBrain "
            f"never chains rates through a third currency"
        )

    def _convert(self, amount: Decimal, *, from_currency: str, to_currency: str) -> Decimal:
        """Multiply or **divide** -- never multiply by a precomputed reciprocal.

        ``amount * (1 / rate)`` and ``amount / rate`` are not the same number in
        ``Decimal``: the reciprocal is rounded to the context's precision first
        and the error is then scaled by the amount. Dividing once keeps the
        round trip exact, which is what makes
        ``to_account_currency(to_instrument_currency(x)) == x`` hold -- and that
        identity is what lets a converted cap be compared against the cap it
        came from.
        """
        direction = self._direction_for(from_currency=from_currency, to_currency=to_currency)
        if direction is FxDirection.IDENTITY:
            return amount
        assert self.rate is not None  # guaranteed by _direction_for
        if direction is FxDirection.DIRECT:
            return amount * self.rate
        return amount / self.rate

    def to_instrument_currency(self, amount: Decimal) -> Decimal:
        """Convert an account-currency amount into the instrument's currency.

        This is the direction that matters for sizing: every cap is denominated
        in the account currency and every price is denominated in the
        instrument's, so the caps have to move to the price rather than the
        other way round -- dividing a GBP ceiling by a USD ask is the specific
        arithmetic Phase 6 refused to do.
        """
        return self._convert(
            amount,
            from_currency=self.account_currency,
            to_currency=self.instrument_currency,
        )

    def to_account_currency(self, amount: Decimal) -> Decimal:
        """Convert an instrument-currency amount into the account's currency.

        Used for the recorded notional and the cash impact, which are the
        numbers every cap and every portfolio percentage is measured against.
        """
        return self._convert(
            amount,
            from_currency=self.instrument_currency,
            to_currency=self.account_currency,
        )

    def direction(self) -> FxDirection:
        """Which way the sizing conversion used the measured pair."""
        if self.same_currency:
            return FxDirection.IDENTITY
        return self._direction_for(
            from_currency=self.account_currency, to_currency=self.instrument_currency
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_currency": self.account_currency,
            "instrument_currency": self.instrument_currency,
            "same_currency": self.same_currency,
            "usable": self.usable,
            "blockers": list(self.blockers),
            "rate": _opt_str(self.rate),
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "pair": self.pair,
            "provider": self.provider,
            "grade": self.grade.value if self.grade else None,
            "rate_type": self.rate_type,
            "direction": (self.direction().value if self.same_currency or self.usable else None),
            "provider_timestamp": (
                self.provider_timestamp.isoformat() if self.provider_timestamp else None
            ),
            "provider_timestamp_precision": self.provider_timestamp_precision,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "age_seconds": _opt_str(self.age_seconds),
        }

    @classmethod
    def same_currency_snapshot(cls, currency: str) -> FxSnapshot:
        """The snapshot for "the instrument and the account agree"."""
        code = normalize_currency(currency)
        return cls(
            account_currency=code,
            instrument_currency=code,
            same_currency=True,
            provider="identity",
        )


@dataclass(frozen=True, slots=True)
class ReservedExposure:
    """What other live proposals have already claimed.

    Without this, two proposals generated seconds apart each believe the whole
    cash buffer is theirs, and the pair together breaches every cap that each
    individually respected.
    """

    count: int = 0
    notional: Decimal = ZERO
    """Reserved cash in account currency, never in a listing's currency."""
    same_instrument_count: int = 0
    same_instrument_notional: Decimal = ZERO
    same_instrument_sides: tuple[str, ...] = ()
    """The sides of the live proposals on this listing. Named for what it is:
    whether any of them *conflicts* depends on the side being proposed, which is
    the rule's business rather than this snapshot's."""

    blockers: tuple[str, ...] = ()
    """Unpriced or inconsistent reservations must never become zero exposure."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "blockers": list(self.blockers),
            "active_proposals": self.count,
            "reserved_notional": str(self.notional),
            "same_instrument_count": self.same_instrument_count,
            "same_instrument_notional": str(self.same_instrument_notional),
            "same_instrument_sides": list(self.same_instrument_sides),
        }


@dataclass(frozen=True, slots=True)
class RiskInputs:
    """Everything the engine is allowed to read.

    Note what is *absent*: no research text, no model-supplied quantity, no
    broker client, no network.  The research layer contributes exactly two
    scalars -- an action and a confidence -- and confidence may only shrink a
    size inside the hard caps.
    """

    config: RiskConfig
    action: ThesisAction
    confidence: Decimal
    identity: InstrumentIdentity
    account: AccountState | None
    quote: QuoteSnapshot | None
    reserved: ReservedExposure
    now: dt.datetime
    fx: FxSnapshot | None = None
    """The FX facts, or ``None`` when they could not even be assembled.

    ``None`` and "a snapshot carrying blockers" are different failures and both
    block: the first means nothing is known about the currencies, the second
    means something is known and it is not good enough."""

    account_state_missing_reason: str | None = None
    quote_missing_reason: str | None = None
    authorized_fx_rate: Decimal | None = None
    """The rate the *authorized* proposal was sized against, when re-evaluating
    one.  Present only on the revalidation path; its absence is why
    ``fx_rate_drift`` reports ``WARN`` rather than ``PASS`` at generation time --
    there is nothing to have drifted from yet."""


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One rule's verdict, in a shape that can be stored, listed and counted."""

    rule_id: str
    rule_version: int
    outcome: RuleOutcome
    reason: str
    observed: str | None = None
    threshold: str | None = None
    max_notional: Decimal | None = None
    """The cap this rule imposes, when it imposes one."""

    size_factor: Decimal | None = None
    """A multiplicative reduction this rule imposes, when it imposes one."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "observed": self.observed,
            "threshold": self.threshold,
            "max_notional": _opt_str(self.max_notional),
            "size_factor": _opt_str(self.size_factor),
        }


@dataclass(frozen=True, slots=True)
class SizingResult:
    """The deterministic order parameters, or why there are none."""

    side: OrderSide | None
    quantity: Decimal
    target_notional: Decimal
    """The trade's notional in the **instrument's** currency: quantity times the
    marketable price.  This is the number a broker would recognise."""

    max_quantity: Decimal
    max_notional: Decimal
    """The binding cap, in the **account's** currency.  Every ``RISK_*`` money
    limit is denominated there, because that is the currency the portfolio,
    the cash buffer and the concentration percentages are measured in."""

    reference_price: Decimal | None
    currency: str | None
    """The instrument's currency: what ``reference_price`` and
    ``target_notional`` are denominated in."""

    account_currency: str | None = None
    notional_account_currency: Decimal = ZERO
    """``target_notional`` converted into the account currency.

    Recorded separately rather than replacing ``target_notional`` because both
    are true and they answer different questions: what the broker will trade,
    and what it costs the portfolio.  Conflating them is how a USD number ends
    up being compared against a GBP cap."""

    max_notional_instrument_currency: Decimal = ZERO
    """``max_notional`` converted into the instrument's currency -- the number
    the quantity was actually derived from.  Persisted so a size can be
    re-checked without re-deriving the conversion."""

    reasons: tuple[str, ...] = ()
    executable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side.value if self.side else None,
            "quantity": str(self.quantity),
            "target_notional": str(self.target_notional),
            "max_quantity": str(self.max_quantity),
            "max_notional": str(self.max_notional),
            "reference_price": _opt_str(self.reference_price),
            "currency": self.currency,
            "account_currency": self.account_currency,
            "notional_account_currency": str(self.notional_account_currency),
            "max_notional_instrument_currency": str(self.max_notional_instrument_currency),
            "reasons": list(self.reasons),
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The complete, persistable verdict."""

    outcome: RiskOutcome
    policy_version: str
    rules: tuple[RuleResult, ...]
    sizing: SizingResult
    inputs_summary: dict[str, Any]
    evaluated_at: dt.datetime

    @property
    def allowed(self) -> bool:
        """True only when nothing blocked *and* a real order came out.

        ``REDUCE_SIZE`` is an allow: the caps did their job.  A decision with no
        executable size is not, however reduced it was.
        """
        return self.outcome is not RiskOutcome.BLOCK and self.sizing.executable

    @property
    def blocks(self) -> tuple[str, ...]:
        return tuple(rule.reason for rule in self.rules if rule.outcome is RuleOutcome.BLOCK)

    @property
    def block_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.rules if rule.outcome is RuleOutcome.BLOCK)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(rule.reason for rule in self.rules if rule.outcome is RuleOutcome.WARN)

    @property
    def reductions(self) -> tuple[str, ...]:
        return tuple(rule.reason for rule in self.rules if rule.outcome is RuleOutcome.REDUCE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "allowed": self.allowed,
            "policy_version": self.policy_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "rules": [rule.as_dict() for rule in self.rules],
            "blocks": list(self.blocks),
            "warnings": list(self.warnings),
            "reductions": list(self.reductions),
            "sizing": self.sizing.as_dict(),
            "inputs": self.inputs_summary,
        }

    def snapshot_hash(self) -> str:
        """Content hash of the whole decision, for tamper-evident audit."""
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _opt_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
