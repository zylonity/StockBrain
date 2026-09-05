"""The individual deterministic risk rules.

Each rule is a small pure function with a stable ``rule_id`` and its own
``rule_version``.  Both are persisted with the proposal, so a decision made a
year ago can still be read against the rule that made it even after the rule
has been rewritten.

The rules fall into two kinds and the difference is load-bearing:

* **Gates** answer yes/no.  A gate that says ``BLOCK`` is final -- no cap, no
  confidence, no execution policy and no operator flag converts it into
  permission.  This is the property that makes "an LLM cannot authorise a
  trade" structural rather than aspirational.
* **Caps** answer "how much".  Each computes a notional ceiling from the
  account snapshot; the engine takes the minimum.  A cap that computes a
  non-positive ceiling has become a gate, and blocks.

Risk-*reducing* actions (``SELL``, ``REDUCE``) deliberately skip the exposure
caps.  Those limits bound risk taken, not risk removed, and a cap that can stop
a position from being closed is a hazard rather than a control.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from stockbrain.enums import (
    MarketSession,
    ResolutionStatus,
    RuleOutcome,
    SpreadStatus,
    ThesisAction,
)
from stockbrain.risk.config import SpreadPolicy
from stockbrain.risk.models import ZERO, RiskInputs, RuleResult

__all__ = [
    "EXPOSURE_INCREASING_ACTIONS",
    "NotionalCap",
    "action_is_executable",
    "cap_rule_results",
    "confidence_size_factor",
    "gate_results",
    "notional_caps",
    "proposal_ttl",
    "reference_price_drift",
]

#: Actions that add exposure.  Only these are subject to the portfolio caps.
EXPOSURE_INCREASING_ACTIONS: frozenset[ThesisAction] = frozenset({ThesisAction.BUY})


@dataclass(frozen=True, slots=True)
class NotionalCap:
    """One rule's ceiling on the notional of this trade."""

    rule_id: str
    rule_version: int
    limit: Decimal
    observed: str
    threshold: str
    reason: str
    defines_intent: bool = False
    """True for the two rules that *set* the intended trade size rather than
    trimming it.  Distinguishing them is what lets the engine say "this trade
    was reduced by the cash buffer" instead of "some cap applied"."""


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def gate_results(inputs: RiskInputs) -> list[RuleResult]:
    """Every yes/no rule, in a stable order.

    Ordering is by dependency, not severity: identity before pricing, pricing
    before portfolio, so a proposal for a delisted instrument reports "the
    listing is gone" rather than "the quote is stale for a listing that no
    longer exists".
    """
    return [
        action_is_executable(inputs.action),
        _instrument_identity(inputs),
        _instrument_type_supported(inputs),
        _account_state_available(inputs),
        _account_state_freshness(inputs),
        _quote_available(inputs),
        _price_source_execution_grade(inputs),
        _quote_freshness(inputs),
        _quote_two_sided(inputs),
        _spread_ceiling(inputs),
        _market_session(inputs),
        _currency_alignment(inputs),
        _current_position(inputs),
        _duplicate_or_conflicting_proposal(inputs),
        _max_active_proposals(inputs),
        _research_confidence_floor(inputs),
    ]


def action_is_executable(action: ThesisAction) -> RuleResult:
    """``HOLD`` and ``NO_ACTION`` are conclusions, not orders.

    Exposed separately from :func:`gate_results` so the proposal service can
    record a durable refusal for an advisory HOLD without spending a
    market-data request to reach the same answer.
    """
    executable = action not in {ThesisAction.HOLD, ThesisAction.NO_ACTION}
    return RuleResult(
        rule_id="action_is_executable",
        rule_version=1,
        outcome=RuleOutcome.PASS if executable else RuleOutcome.BLOCK,
        reason=(
            f"{action.value} is an executable action"
            if executable
            else f"{action.value} produces no executable order"
        ),
        observed=action.value,
        threshold="BUY, SELL or REDUCE",
    )


def _instrument_identity(inputs: RiskInputs) -> RuleResult:
    identity = inputs.identity
    problems: list[str] = []
    if identity.resolution_status is not ResolutionStatus.RESOLVED:
        problems.append(f"resolution is {identity.resolution_status.value}, not RESOLVED")
    if not identity.is_active:
        problems.append("the broker listing is no longer active")
    if not identity.broker_ticker:
        problems.append("no broker ticker")
    if not identity.market_symbol:
        problems.append("no market-data symbol, so the listing cannot be priced")
    if identity.company_id is None:
        problems.append("no verified company is attached to the listing")
    return RuleResult(
        rule_id="instrument_identity",
        rule_version=1,
        outcome=RuleOutcome.BLOCK if problems else RuleOutcome.PASS,
        reason=(
            "instrument identity is no longer valid: " + "; ".join(problems)
            if problems
            else f"{identity.broker_ticker} is a RESOLVED, active broker listing"
        ),
        observed=identity.resolution_status.value,
        threshold=ResolutionStatus.RESOLVED.value,
    )


def _instrument_type_supported(inputs: RiskInputs) -> RuleResult:
    allowed = inputs.config.allowed_instrument_types
    observed = (inputs.identity.instrument_type or "").upper()
    ok = observed in {item.upper() for item in allowed}
    return RuleResult(
        rule_id="instrument_type_supported",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"instrument type {observed or 'UNKNOWN'} is supported"
            if ok
            else f"instrument type {observed or 'UNKNOWN'} is not one of {', '.join(allowed)}"
        ),
        observed=observed or None,
        threshold=", ".join(allowed),
    )


def _account_state_available(inputs: RiskInputs) -> RuleResult:
    available = inputs.account is not None
    return RuleResult(
        rule_id="account_state_available",
        rule_version=1,
        outcome=RuleOutcome.PASS if available else RuleOutcome.BLOCK,
        reason=(
            "broker account state is available"
            if available
            else (
                "broker account state is unavailable: "
                f"{inputs.account_state_missing_reason or 'no snapshot'}"
            )
        ),
        observed="available" if available else "missing",
        threshold="available",
    )


def _account_state_freshness(inputs: RiskInputs) -> RuleResult:
    limit = inputs.config.max_account_state_age_seconds
    if inputs.account is None:
        return RuleResult(
            rule_id="account_state_freshness",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="account state age cannot be established without a snapshot",
            threshold=f"{limit}s",
        )
    age = inputs.account.age_seconds(inputs.now)
    ok = age <= limit
    return RuleResult(
        rule_id="account_state_freshness",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"account state is {age:.0f}s old, within the {limit}s limit"
            if ok
            else f"account state is {age:.0f}s old, older than the {limit}s limit"
        ),
        observed=f"{age:.0f}s",
        threshold=f"{limit}s",
    )


def _quote_available(inputs: RiskInputs) -> RuleResult:
    available = inputs.quote is not None
    return RuleResult(
        rule_id="quote_available",
        rule_version=1,
        outcome=RuleOutcome.PASS if available else RuleOutcome.BLOCK,
        reason=(
            "a fresh quote was fetched for this listing"
            if available
            else f"no quote available: {inputs.quote_missing_reason or 'provider returned nothing'}"
        ),
        observed="available" if available else "missing",
        threshold="available",
    )


def _price_source_execution_grade(inputs: RiskInputs) -> RuleResult:
    quote = inputs.quote
    if quote is None:
        return _skipped("price_source_execution_grade", 1, "no quote to judge")
    blockers = tuple(quote.provider_blockers)
    ok = not blockers
    return RuleResult(
        rule_id="price_source_execution_grade",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"{quote.price_source.value} on feed {quote.feed} is execution-grade"
            if ok
            else "; ".join(blockers)
        ),
        observed=quote.price_source.value,
        threshold="ALPACA_IEX or ALPACA_SIP",
    )


def _quote_freshness(inputs: RiskInputs) -> RuleResult:
    quote = inputs.quote
    limit = inputs.config.max_quote_age_seconds
    if quote is None:
        return _skipped("quote_freshness", 1, "no quote to judge", threshold=f"{limit}s")
    age_seconds = Decimal(quote.age_ms) / Decimal(1000)
    ok = age_seconds <= limit
    return RuleResult(
        rule_id="quote_freshness",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"quote is {quote.age_ms}ms old, within the {limit}s limit"
            if ok
            else f"quote is {quote.age_ms}ms old, older than the {limit}s limit"
        ),
        observed=f"{quote.age_ms}ms",
        threshold=f"{limit}s",
    )


def _quote_two_sided(inputs: RiskInputs) -> RuleResult:
    quote = inputs.quote
    if quote is None:
        return _skipped("quote_two_sided", 1, "no quote to judge")
    status = quote.spread.status
    ok = status not in {SpreadStatus.MISSING, SpreadStatus.NON_POSITIVE, SpreadStatus.ONE_SIDED}
    return RuleResult(
        rule_id="quote_two_sided",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=("quote has a live bid and a live ask" if ok else quote.spread.detail),
        observed=status.value,
        threshold="two-sided",
    )


def _spread_ceiling(inputs: RiskInputs) -> RuleResult:
    """The hard bid/ask ceiling.

    A crossed, locked, one-sided or absent book blocks under every policy: it
    has no usable mid, so there is nothing to size against.  Only a well-formed
    book that is merely *too wide* is eligible for the ``REDUCE`` policy, and
    even then reducing is an explicit configuration choice.
    """
    quote = inputs.quote
    config = inputs.config
    if quote is None:
        return _skipped(
            "spread_ceiling", 1, "no quote to judge", threshold=f"{config.max_spread_bps} bps"
        )
    assessment = quote.spread
    if assessment.is_ok:
        return RuleResult(
            rule_id="spread_ceiling",
            rule_version=1,
            outcome=RuleOutcome.PASS,
            reason=assessment.detail,
            observed=str(assessment.spread_bps) if assessment.spread_bps is not None else None,
            threshold=f"{config.max_spread_bps} bps",
        )
    reduce_allowed = (
        assessment.is_merely_wide
        and config.spread_policy == SpreadPolicy.REDUCE
        and config.wide_spread_size_factor > ZERO
    )
    return RuleResult(
        rule_id="spread_ceiling",
        rule_version=1,
        outcome=RuleOutcome.REDUCE if reduce_allowed else RuleOutcome.BLOCK,
        reason=(
            f"{assessment.detail}; policy reduces size by "
            f"{config.wide_spread_size_factor} rather than blocking"
            if reduce_allowed
            else assessment.detail
        ),
        observed=str(assessment.spread_bps) if assessment.spread_bps is not None else None,
        threshold=f"{config.max_spread_bps} bps",
        size_factor=config.wide_spread_size_factor if reduce_allowed else None,
    )


def _market_session(inputs: RiskInputs) -> RuleResult:
    quote = inputs.quote
    allowed = inputs.config.allowed_sessions
    allowed_names = ", ".join(item.value for item in allowed)
    if quote is None:
        return _skipped("market_session", 1, "no quote to judge", threshold=allowed_names)
    session = quote.session
    if session is MarketSession.UNKNOWN and inputs.config.require_known_session:
        return RuleResult(
            rule_id="market_session",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason=(
                "the trading session is unknown for this listing "
                f"(source: {quote.session_source}); an unknown session is not a tradable one"
            ),
            observed=session.value,
            threshold=allowed_names,
        )
    ok = session in allowed
    return RuleResult(
        rule_id="market_session",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"session {session.value} is permitted (source: {quote.session_source}"
            f"{', holiday-aware' if quote.session_holiday_aware else ''})"
            if ok
            else f"session {session.value} is not one of {allowed_names}"
        ),
        observed=session.value,
        threshold=allowed_names,
    )


def _currency_alignment(inputs: RiskInputs) -> RuleResult:
    """Cross-currency sizing is refused rather than guessed.

    Trading 212 documents that orders execute only in the primary account
    currency and that multi-currency accounts are not supported through the
    API.  StockBrain has no verified FX source, and a size computed from an
    invented rate is a wrong size.  So v1 is same-currency only, enforced here
    and stated in the refusal.
    """
    identity_currency = (inputs.identity.currency or "").upper()
    quote_currency = (inputs.quote.currency or "").upper() if inputs.quote else ""
    account_currency = (inputs.account.currency or "").upper() if inputs.account else ""
    if not inputs.config.require_same_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=1,
            outcome=RuleOutcome.WARN,
            reason="cross-currency sizing is permitted by configuration but has no FX source",
            observed=f"instrument {identity_currency} / account {account_currency}",
            threshold="same currency",
        )
    if not account_currency or not identity_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="instrument or account currency is unknown, so sizing cannot be reconciled",
            observed=f"instrument {identity_currency or '?'} / account {account_currency or '?'}",
            threshold="same currency",
        )
    mismatches = [
        f"instrument {identity_currency} != account {account_currency}"
        if identity_currency != account_currency
        else "",
        f"quote {quote_currency} != account {account_currency}"
        if quote_currency and quote_currency != account_currency
        else "",
    ]
    problems = [item for item in mismatches if item]
    return RuleResult(
        rule_id="currency_alignment",
        rule_version=1,
        outcome=RuleOutcome.BLOCK if problems else RuleOutcome.PASS,
        reason=(
            "cross-currency sizing is unsupported and no FX rate source is configured: "
            + "; ".join(problems)
            if problems
            else f"instrument, quote and account are all denominated in {account_currency}"
        ),
        observed=f"instrument {identity_currency} / quote {quote_currency or '?'} "
        f"/ account {account_currency}",
        threshold="same currency",
    )


def _current_position(inputs: RiskInputs) -> RuleResult:
    """What the account already holds, judged against what the action needs."""
    account = inputs.account
    if account is None:
        return _skipped("current_position", 1, "no account state to judge")
    position = account.position(inputs.identity.broker_ticker)
    held = position.quantity if position else ZERO
    available = position.quantity_available if position else ZERO

    if inputs.action in {ThesisAction.SELL, ThesisAction.REDUCE}:
        if available <= ZERO:
            return RuleResult(
                rule_id="current_position",
                rule_version=1,
                outcome=RuleOutcome.BLOCK,
                reason=(
                    f"{inputs.action.value} requires an existing long position; "
                    f"{held} held, {available} available for trading and short selling is disabled"
                ),
                observed=f"{available} available",
                threshold="> 0",
            )
        return RuleResult(
            rule_id="current_position",
            rule_version=1,
            outcome=RuleOutcome.PASS,
            reason=f"{available} of {held} shares are available to reduce",
            observed=f"{available} available",
            threshold="> 0",
        )

    return RuleResult(
        rule_id="current_position",
        rule_version=1,
        outcome=RuleOutcome.PASS,
        reason=(f"currently holding {held} shares" if held > ZERO else "no existing position"),
        observed=str(held),
        threshold="n/a",
    )


def _duplicate_or_conflicting_proposal(inputs: RiskInputs) -> RuleResult:
    """One live proposal per listing, and never two that disagree.

    A duplicate and a contradiction are reported separately because they are
    different mistakes: the first is a redelivered job, the second is two
    theses pulling in opposite directions on the same security, which is a
    correctness problem for a human rather than a queue artefact.
    """
    from stockbrain.risk.sizing import ACTION_SIDES

    reserved = inputs.reserved
    side = ACTION_SIDES.get(inputs.action)
    opposing = tuple(
        item for item in reserved.same_instrument_sides if side is not None and item != side.value
    )
    problems: list[str] = []
    if reserved.same_instrument_count:
        problems.append(
            f"{reserved.same_instrument_count} live proposal(s) already exist for "
            f"{inputs.identity.broker_ticker}"
        )
    if opposing:
        problems.append(
            f"a live proposal for the same listing takes the opposite side ({', '.join(opposing)})"
        )
    return RuleResult(
        rule_id="duplicate_or_conflicting_proposal",
        rule_version=1,
        outcome=RuleOutcome.BLOCK if problems else RuleOutcome.PASS,
        reason=("; ".join(problems) if problems else "no live proposal exists for this listing"),
        observed=str(reserved.same_instrument_count),
        threshold="0",
    )


def _max_active_proposals(inputs: RiskInputs) -> RuleResult:
    limit = inputs.config.max_active_proposals
    count = inputs.reserved.count
    ok = count < limit
    return RuleResult(
        rule_id="max_active_proposals",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"{count} of {limit} active proposal slots in use"
            if ok
            else f"{count} active proposals already reach the limit of {limit}"
        ),
        observed=str(count),
        threshold=str(limit),
    )


def _research_confidence_floor(inputs: RiskInputs) -> RuleResult:
    """A bounded floor on the research layer's own confidence.

    Confidence is a ranking feature, not a calibrated probability, so it is only
    ever used in the conservative direction: below the floor nothing is
    proposed, and above it confidence may shrink -- never grow -- a size that
    the hard caps already permit.
    """
    floor = inputs.config.min_research_confidence
    ok = inputs.confidence >= floor
    return RuleResult(
        rule_id="research_confidence_floor",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"research confidence {inputs.confidence} meets the {floor} floor"
            if ok
            else f"research confidence {inputs.confidence} is below the {floor} floor"
        ),
        observed=str(inputs.confidence),
        threshold=str(floor),
    )


def _skipped(
    rule_id: str, version: int, reason: str, *, threshold: str | None = None
) -> RuleResult:
    """A rule that could not run because an earlier gate already blocked.

    Reported as ``WARN`` rather than ``PASS``: a rule that did not run has not
    passed, and recording it as a pass would make a blocked decision look
    partially approved.
    """
    return RuleResult(
        rule_id=rule_id,
        rule_version=version,
        outcome=RuleOutcome.WARN,
        reason=f"not evaluated: {reason}",
        observed=None,
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
def notional_caps(inputs: RiskInputs) -> list[NotionalCap]:
    """Every ceiling on this trade's notional, in the account currency.

    Returns an empty list for risk-reducing actions: exposure limits bound risk
    taken, not risk removed.
    """
    if inputs.action not in EXPOSURE_INCREASING_ACTIONS:
        return []
    account = inputs.account
    if account is None:
        return []

    config = inputs.config
    total = account.total_value
    caps: list[NotionalCap] = [
        NotionalCap(
            rule_id="max_notional_per_trade",
            rule_version=1,
            limit=config.max_notional_per_trade,
            observed=str(config.max_notional_per_trade),
            threshold=str(config.max_notional_per_trade),
            reason=f"absolute per-trade ceiling of {config.max_notional_per_trade}",
            defines_intent=True,
        ),
        NotionalCap(
            rule_id="max_trade_pct_of_portfolio",
            rule_version=1,
            limit=_pct(total, config.max_trade_pct_of_portfolio),
            observed=str(total),
            threshold=str(config.max_trade_pct_of_portfolio),
            reason=(
                f"{config.max_trade_pct_of_portfolio} of the {total} {account.currency} "
                "portfolio may be committed to one trade"
            ),
            defines_intent=True,
        ),
    ]

    # Concentration: how much more of this name may be held in total.
    position = account.position(inputs.identity.broker_ticker)
    held_value = (position.market_value or ZERO) if position else ZERO
    concentration_headroom = _pct(total, config.max_position_pct_of_portfolio) - held_value
    caps.append(
        NotionalCap(
            rule_id="max_position_concentration",
            rule_version=1,
            limit=max(ZERO, concentration_headroom),
            observed=str(held_value),
            threshold=str(_pct(total, config.max_position_pct_of_portfolio)),
            reason=(
                f"{inputs.identity.broker_ticker} may reach "
                f"{config.max_position_pct_of_portfolio} of the portfolio; "
                f"{held_value} is already held"
            ),
        )
    )

    # Aggregate exposure across the whole book.
    aggregate_headroom = _pct(total, config.max_aggregate_exposure_pct) - account.invested_value
    caps.append(
        NotionalCap(
            rule_id="max_aggregate_exposure",
            rule_version=1,
            limit=max(ZERO, aggregate_headroom),
            observed=str(account.invested_value),
            threshold=str(_pct(total, config.max_aggregate_exposure_pct)),
            reason=(
                f"aggregate invested value may reach {config.max_aggregate_exposure_pct} "
                f"of the portfolio; {account.invested_value} is already invested"
            ),
        )
    )

    # Exposure other live proposals have already reserved.
    proposal_headroom = (
        _pct(total, config.max_active_proposal_exposure_pct) - inputs.reserved.notional
    )
    caps.append(
        NotionalCap(
            rule_id="active_proposal_exposure",
            rule_version=1,
            limit=max(ZERO, proposal_headroom),
            observed=str(inputs.reserved.notional),
            threshold=str(_pct(total, config.max_active_proposal_exposure_pct)),
            reason=(
                f"{inputs.reserved.count} live proposal(s) already reserve "
                f"{inputs.reserved.notional} of the "
                f"{config.max_active_proposal_exposure_pct} proposal exposure budget"
            ),
        )
    )

    # Cash, less the buffer and less what other proposals have already claimed.
    reserve = _pct(total, config.min_cash_reserve_pct)
    spendable = account.cash_available - reserve - inputs.reserved.notional
    caps.append(
        NotionalCap(
            rule_id="min_cash_reserve",
            rule_version=1,
            limit=max(ZERO, spendable),
            observed=str(account.cash_available),
            threshold=str(reserve),
            reason=(
                f"{account.cash_available} cash available, keeping a {reserve} reserve "
                f"({config.min_cash_reserve_pct} of portfolio) and honouring "
                f"{inputs.reserved.notional} already reserved by live proposals"
            ),
        )
    )

    # The broker's own maximum open quantity for this instrument.
    max_open = inputs.identity.max_open_quantity
    price = inputs.quote.mid if inputs.quote else None
    if max_open is not None and price is not None and price > ZERO:
        held_quantity = position.quantity if position else ZERO
        remaining = max(ZERO, max_open - held_quantity)
        caps.append(
            NotionalCap(
                rule_id="broker_max_open_quantity",
                rule_version=1,
                limit=remaining * price,
                observed=str(held_quantity),
                threshold=str(max_open),
                reason=(
                    f"the broker caps open quantity at {max_open} for "
                    f"{inputs.identity.broker_ticker}; {held_quantity} is already held"
                ),
            )
        )
    return caps


def _pct(total: Decimal, fraction: Decimal) -> Decimal:
    return total * fraction


def cap_rule_results(caps: Iterable[NotionalCap], intended: Decimal) -> list[RuleResult]:
    """Turn caps into persisted rule results.

    A cap that computed no headroom at all is a ``BLOCK``: there is no size at
    which this trade respects it.  A cap below the intended trade size is a
    ``REDUCE``.  Anything else passed.
    """
    results: list[RuleResult] = []
    for cap in caps:
        if cap.limit <= ZERO:
            outcome = RuleOutcome.BLOCK
        elif not cap.defines_intent and cap.limit < intended:
            outcome = RuleOutcome.REDUCE
        else:
            outcome = RuleOutcome.PASS
        results.append(
            RuleResult(
                rule_id=cap.rule_id,
                rule_version=cap.rule_version,
                outcome=outcome,
                reason=(
                    f"{cap.reason}; no headroom remains"
                    if outcome is RuleOutcome.BLOCK
                    else f"{cap.reason}; caps this trade at {cap.limit}"
                    if outcome is RuleOutcome.REDUCE
                    else cap.reason
                ),
                observed=cap.observed,
                threshold=cap.threshold,
                max_notional=cap.limit,
            )
        )
    return results


def confidence_size_factor(inputs: RiskInputs) -> RuleResult | None:
    """Scale the size with research confidence, inside the hard caps.

    Returns ``None`` when modulation is disabled.  The factor is linear between
    the configured floor (which maps to ``min_confidence_size_factor``) and 1.0
    (which maps to no reduction), and is clamped to ``[factor_floor, 1]`` so it
    can only ever shrink a position.  Confidence never lifts a cap and never
    lifts a block; it is a ranking feature, not a probability.
    """
    config = inputs.config
    if not config.confidence_modulates_size:
        return None
    floor = config.min_research_confidence
    factor_floor = config.min_confidence_size_factor
    span = Decimal(1) - floor
    if span <= ZERO:
        factor = Decimal(1)
    else:
        progress = (inputs.confidence - floor) / span
        factor = factor_floor + (Decimal(1) - factor_floor) * progress
    factor = min(Decimal(1), max(factor_floor, factor))
    return RuleResult(
        rule_id="confidence_size_modulation",
        rule_version=1,
        outcome=RuleOutcome.REDUCE if factor < Decimal(1) else RuleOutcome.PASS,
        reason=(
            f"research confidence {inputs.confidence} scales the size to {factor} "
            "of the deterministic maximum (it can only reduce, never authorise)"
        ),
        observed=str(inputs.confidence),
        threshold=f"[{factor_floor}, 1]",
        size_factor=factor,
    )


def reference_price_drift(
    reference_price: Decimal,
    current_price: Decimal | None,
    *,
    max_drift_pct: Decimal,
) -> RuleResult:
    """Whether the market has moved away from a proposal's reference price.

    Used at authorization time and by the invalidation sweep.  A proposal whose
    quantity was computed against a price that no longer exists is not the trade
    the operator is looking at.
    """
    if current_price is None or reference_price <= ZERO:
        return RuleResult(
            rule_id="reference_price_drift",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="the reference price cannot be compared against a current price",
            threshold=str(max_drift_pct),
        )
    drift = abs(current_price - reference_price) / reference_price
    ok = drift <= max_drift_pct
    return RuleResult(
        rule_id="reference_price_drift",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"the market has moved {drift:.4f} from the {reference_price} reference price, "
            f"within the {max_drift_pct} limit"
            if ok
            else (
                f"the market has moved {drift:.4f} from the {reference_price} reference price, "
                f"beyond the {max_drift_pct} limit; the proposal no longer describes this trade"
            )
        ),
        observed=str(drift.quantize(Decimal("0.000001"))),
        threshold=str(max_drift_pct),
    )


def proposal_ttl(expires_at: dt.datetime, now: dt.datetime) -> RuleResult:
    expired = expires_at <= now
    return RuleResult(
        rule_id="proposal_ttl",
        rule_version=1,
        outcome=RuleOutcome.BLOCK if expired else RuleOutcome.PASS,
        reason=(
            f"the proposal expired at {expires_at.isoformat()}"
            if expired
            else f"the proposal is valid until {expires_at.isoformat()}"
        ),
        observed=now.isoformat(),
        threshold=expires_at.isoformat(),
    )
