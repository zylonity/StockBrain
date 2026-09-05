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
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.spread import SpreadAssessment

__all__ = [
    "ZERO",
    "AccountState",
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
class ReservedExposure:
    """What other live proposals have already claimed.

    Without this, two proposals generated seconds apart each believe the whole
    cash buffer is theirs, and the pair together breaches every cap that each
    individually respected.
    """

    count: int = 0
    notional: Decimal = ZERO
    same_instrument_count: int = 0
    same_instrument_notional: Decimal = ZERO
    same_instrument_sides: tuple[str, ...] = ()
    """The sides of the live proposals on this listing. Named for what it is:
    whether any of them *conflicts* depends on the side being proposed, which is
    the rule's business rather than this snapshot's."""

    def as_dict(self) -> dict[str, Any]:
        return {
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
    account_state_missing_reason: str | None = None
    quote_missing_reason: str | None = None


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
    max_quantity: Decimal
    max_notional: Decimal
    reference_price: Decimal | None
    currency: str | None
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
