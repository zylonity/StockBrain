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
    TRANSIENT_RULE_IDS,
    MarketSession,
    ResolutionStatus,
    RuleOutcome,
    SpreadStatus,
    ThesisAction,
)
from stockbrain.risk.config import SpreadPolicy
from stockbrain.risk.models import ZERO, FxSnapshot, RiskInputs, RuleResult

__all__ = [
    "EXPOSURE_INCREASING_ACTIONS",
    "RISK_REDUCING_ACTIONS",
    "TRANSIENT_RULE_IDS",
    "NotionalCap",
    "action_is_executable",
    "calibration_size_factor",
    "cap_rule_results",
    "confidence_size_factor",
    "fx_rate_drift",
    "gate_results",
    "notional_caps",
    "proposal_ttl",
    "reference_price_drift",
]

#: Actions that add exposure.  Only these are subject to the portfolio caps.
EXPOSURE_INCREASING_ACTIONS: frozenset[ThesisAction] = frozenset({ThesisAction.BUY})

#: Actions that remove exposure.  Exempt from the caps and the confidence
#: floor: a control that can stop a position being closed is a hazard.
RISK_REDUCING_ACTIONS: frozenset[ThesisAction] = frozenset({ThesisAction.SELL, ThesisAction.REDUCE})

# ``TRANSIENT_RULE_IDS`` is defined in :mod:`stockbrain.enums` and re-exported
# here under its canonical risk-side name; see that definition for why it lives
# in the dependency-free vocabulary module.


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
        _fx_available(inputs),
        _fx_freshness(inputs),
        _current_position(inputs),
        _duplicate_or_conflicting_proposal(inputs),
        _max_active_proposals(inputs),
        _reservation_accounting(inputs),
        _research_confidence_floor(inputs),
    ]


def _reservation_accounting(inputs: RiskInputs) -> RuleResult:
    needed = inputs.action in EXPOSURE_INCREASING_ACTIONS
    blocked = needed and bool(inputs.reserved.blockers)
    return RuleResult(
        rule_id="reservation_accounting",
        rule_version=1,
        outcome=RuleOutcome.BLOCK if blocked else RuleOutcome.PASS,
        reason=(
            "; ".join(inputs.reserved.blockers)
            if blocked
            else "reservations are priced in account currency"
            if needed
            else "reductions do not consume reserved cash"
        ),
        threshold="all increasing exposure priced in account currency",
    )


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
    """Whether this deployment permits the instrument and the account to differ.

    A *policy* question, deliberately separate from "is there a rate".
    ``RISK_REQUIRE_SAME_CURRENCY`` is the operator saying whether cross-currency
    sizing is allowed at all; :func:`_fx_available` and :func:`_fx_freshness`
    then say whether it can actually be done. Conflating the two is what let
    Phase 6's version return ``WARN`` for a permitted mismatch and then hand
    sizing a GBP ceiling to divide by a USD ask -- a wrong quantity, silently,
    with a warning nobody had to act on.

    Version 2: the permitted branch is now a ``PASS`` that names the FX gates as
    the thing standing behind it, and there is no branch that permits a mismatch
    without one.
    """
    identity_currency = (inputs.identity.currency or "").upper()
    quote_currency = (inputs.quote.currency or "").upper() if inputs.quote else ""
    account_currency = (inputs.account.currency or "").upper() if inputs.account else ""

    if not account_currency or not identity_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=2,
            outcome=RuleOutcome.BLOCK,
            reason="instrument or account currency is unknown, so sizing cannot be reconciled",
            observed=f"instrument {identity_currency or '?'} / account {account_currency or '?'}",
            threshold="same currency",
        )

    mismatches = [
        f"instrument {identity_currency} != account {account_currency}"
        if identity_currency != account_currency
        else "",
        # The quote must agree with the *instrument*, not with the account: a
        # USD listing priced in USD is correct on a GBP account. A quote in a
        # third currency is a resolution failure and blocks either way, because
        # no single rate can reconcile three currencies.
        f"quote {quote_currency} != instrument {identity_currency}"
        if quote_currency and quote_currency != identity_currency
        else "",
    ]
    problems = [item for item in mismatches if item]
    observed = (
        f"instrument {identity_currency} / quote {quote_currency or '?'} "
        f"/ account {account_currency}"
    )

    if quote_currency and quote_currency != identity_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=2,
            outcome=RuleOutcome.BLOCK,
            reason=(
                f"the quote is denominated in {quote_currency} but the listing is "
                f"{identity_currency}; no single FX rate reconciles three currencies"
            ),
            observed=observed,
            threshold="quote currency == instrument currency",
        )

    if identity_currency == account_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=2,
            outcome=RuleOutcome.PASS,
            reason=f"instrument, quote and account are all denominated in {account_currency}",
            observed=observed,
            threshold="same currency",
        )

    if inputs.config.require_same_currency:
        return RuleResult(
            rule_id="currency_alignment",
            rule_version=2,
            outcome=RuleOutcome.BLOCK,
            reason=(
                "RISK_REQUIRE_SAME_CURRENCY is true, so cross-currency sizing is not "
                "permitted by this deployment: " + "; ".join(problems)
            ),
            observed=observed,
            threshold="same currency",
        )

    return RuleResult(
        rule_id="currency_alignment",
        rule_version=2,
        outcome=RuleOutcome.PASS,
        reason=(
            f"cross-currency sizing is permitted by configuration; the "
            f"{account_currency}->{identity_currency} conversion is gated by fx_available "
            f"and fx_freshness"
        ),
        observed=observed,
        threshold="verified FX rate required",
    )


def _fx_available(inputs: RiskInputs) -> RuleResult:
    """Whether a usable FX rate exists for this instrument and account.

    The FX analogue of ``quote_available``, and it fails closed in exactly the
    same way: no rate, the wrong pair, or a source the operator has not
    permitted to size a trade all ``BLOCK``.  Nothing here can produce a
    conversion factor of one for two different currencies -- the snapshot type
    cannot represent that state.

    Staleness is deliberately *not* judged here.  A missing rate and a stale
    rate demand different responses at send time: the first says nothing about
    the trade, the second says this deployment is behind.  Splitting them is
    what lets :mod:`stockbrain.execution.preflight` defer one and retire the
    other.
    """
    account_currency = (inputs.account.currency or "").upper() if inputs.account else ""
    identity_currency = (inputs.identity.currency or "").upper()
    if not account_currency or not identity_currency:
        return _skipped("fx_available", 1, "the account or instrument currency is unknown")
    if account_currency == identity_currency:
        return RuleResult(
            rule_id="fx_available",
            rule_version=1,
            outcome=RuleOutcome.PASS,
            reason=f"no conversion is needed: both sides are {account_currency}",
            observed=f"{account_currency} == {identity_currency}",
            threshold="no conversion required",
        )

    snapshot = inputs.fx
    if snapshot is None:
        return RuleResult(
            rule_id="fx_available",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason=(
                f"no FX facts were supplied for {account_currency}->{identity_currency}; "
                f"a rate is never assumed"
            ),
            observed="none",
            threshold=f"a verified {account_currency}->{identity_currency} rate",
        )
    # Staleness is fx_freshness's rule. Everything else the snapshot objected to
    # belongs here, and an unusable snapshot with only an age objection still
    # blocks -- through the other rule, in the same evaluation.
    structural = tuple(reason for reason in snapshot.blockers if " old, older than " not in reason)
    if structural:
        return RuleResult(
            rule_id="fx_available",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="; ".join(structural),
            observed=snapshot.pair or "none",
            threshold=f"a verified {account_currency}->{identity_currency} rate",
        )
    return RuleResult(
        rule_id="fx_available",
        rule_version=1,
        outcome=RuleOutcome.PASS,
        reason=(
            f"{snapshot.pair} at {snapshot.rate} from {snapshot.provider} "
            f"({snapshot.grade.value.lower() if snapshot.grade else 'unknown'}-grade "
            f"{snapshot.rate_type or 'rate'})"
        ),
        observed=f"{snapshot.pair}={snapshot.rate}",
        threshold=f"a verified {account_currency}->{identity_currency} rate",
    )


def _fx_freshness(inputs: RiskInputs) -> RuleResult:
    """Whether the FX rate is recent enough to size against.

    The limit lives with the provider grade rather than here: an execution-grade
    feed and a daily central-bank fixing are held to different budgets, and
    :func:`stockbrain.fx.base.fx_blockers` is the single place that decides
    which. This rule reports that verdict in the persisted rule vocabulary.
    """
    account_currency = (inputs.account.currency or "").upper() if inputs.account else ""
    identity_currency = (inputs.identity.currency or "").upper()
    if not account_currency or not identity_currency:
        return _skipped("fx_freshness", 1, "the account or instrument currency is unknown")
    if account_currency == identity_currency:
        return RuleResult(
            rule_id="fx_freshness",
            rule_version=1,
            outcome=RuleOutcome.PASS,
            reason="no rate is used, so none can be stale",
            observed="0",
            threshold="no conversion required",
        )
    snapshot = inputs.fx
    if snapshot is None:
        return _skipped("fx_freshness", 1, "no FX facts were supplied")
    stale = [reason for reason in snapshot.blockers if " old, older than " in reason]
    if stale:
        return RuleResult(
            rule_id="fx_freshness",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="; ".join(stale),
            observed=str(snapshot.age_seconds) if snapshot.age_seconds is not None else "unknown",
            threshold="within the configured FX age limit",
        )
    if snapshot.age_seconds is None:
        return _skipped("fx_freshness", 1, "the rate carried no measurable age")
    return RuleResult(
        rule_id="fx_freshness",
        rule_version=1,
        outcome=RuleOutcome.PASS,
        reason=(
            f"the {snapshot.pair} rate is {snapshot.age_seconds}s old, within the limit for a "
            f"{snapshot.grade.value.lower() if snapshot.grade else 'unknown'}-grade source"
        ),
        observed=str(snapshot.age_seconds),
        threshold="within the configured FX age limit",
    )


def _current_position(inputs: RiskInputs) -> RuleResult:
    """What the account already holds, judged against what the action needs."""
    account = inputs.account
    if account is None:
        return _skipped("current_position", 1, "no account state to judge")
    position = account.position(inputs.identity.broker_ticker)
    held = position.quantity if position else ZERO
    available = position.quantity_available if position else ZERO

    if inputs.action in RISK_REDUCING_ACTIONS:
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

    if held > ZERO and position is not None and position.market_value is None:
        return RuleResult(
            rule_id="current_position",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason="the existing position has no validated account-currency valuation",
            observed="unknown",
            threshold="position valued in account currency",
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
    if inputs.action in RISK_REDUCING_ACTIONS:
        return _skipped(
            "max_active_proposals",
            2,
            "the active-proposal cap gates entries; a reduction is never blocked by it",
            threshold=str(limit),
        )
    ok = count < limit
    return RuleResult(
        rule_id="max_active_proposals",
        rule_version=2,
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

    It gates *entries* only.  A floor that could block a SELL would turn a
    lukewarm opening thesis into a reason a position can never be closed, which
    is the hazard this module's header names rather than a control.
    """
    floor = inputs.config.min_research_confidence
    if inputs.action in RISK_REDUCING_ACTIONS:
        return _skipped(
            "research_confidence_floor",
            2,
            "the confidence floor gates entries; a reduction is never blocked by it",
            threshold=str(floor),
        )
    ok = inputs.confidence >= floor
    return RuleResult(
        rule_id="research_confidence_floor",
        rule_version=2,
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
    #
    # This is the one cap derived from a *price* rather than from a portfolio
    # figure, so it arrives in the instrument's currency and has to be converted
    # before it can sit in a list of account-currency ceilings. Phase 6 could
    # skip that because the two currencies were required to match; with FX in
    # play, leaving it unconverted would put a USD number into a GBP minimum and
    # silently become the binding cap.
    max_open = inputs.identity.max_open_quantity
    price = inputs.quote.mid if inputs.quote else None
    fx = inputs.fx
    if max_open is not None and price is not None and price > ZERO:
        held_quantity = position.quantity if position else ZERO
        remaining = max(ZERO, max_open - held_quantity)
        limit_instrument = remaining * price
        limit_account: Decimal | None
        if fx is None or not fx.conversion_required:
            limit_account = limit_instrument
        elif fx.usable:
            limit_account = fx.to_account_currency(limit_instrument)
        else:
            # No usable rate: `fx_available` has already blocked, and inventing
            # a converted ceiling here would be the one thing this whole
            # subsystem exists to prevent. The cap is omitted rather than
            # guessed; the decision is blocked regardless.
            limit_account = None
        if limit_account is not None:
            caps.append(
                NotionalCap(
                    rule_id="broker_max_open_quantity",
                    rule_version=2,
                    limit=limit_account,
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


def calibration_size_factor(inputs: RiskInputs) -> RuleResult | None:
    """Shrink the size when this system's record in the same situation is poor.

    ``None`` when modulation is off or nothing is known.  Below the sample floor
    the rule is reported as not evaluated rather than silently neutral, so a
    reviewer can see the record was too thin to act on.  The factor is
    ``0.5 + hit_rate`` clamped to ``[floor, 1]``: a coin-flip record is neutral,
    a perfect record earns nothing, and no record can lift a cap or a block.
    """
    config = inputs.config
    bucket = inputs.calibration
    if not config.calibration_modulates_size or bucket is None:
        return None
    if bucket.samples < config.calibration_min_samples:
        return _skipped(
            "calibration_size_modulation",
            1,
            f"{bucket.key} has {bucket.samples} of {config.calibration_min_samples} "
            "graded outcomes needed",
            threshold=str(config.calibration_min_samples),
        )
    floor = config.min_calibration_size_factor
    factor = min(Decimal(1), max(floor, Decimal("0.5") + bucket.hit_rate))
    return RuleResult(
        rule_id="calibration_size_modulation",
        rule_version=1,
        outcome=RuleOutcome.REDUCE if factor < Decimal(1) else RuleOutcome.PASS,
        reason=(
            f"{bucket.key}: {bucket.correct}/{bucket.samples} correct, mean alpha "
            f"{bucket.mean_alpha:+.2%}, scales the size to {factor} of the deterministic "
            "maximum (it can only reduce, never authorise)"
        ),
        observed=str(bucket.hit_rate),
        threshold=f"[{floor}, 1]",
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


def fx_rate_drift(
    authorized_rate: Decimal | None,
    current: FxSnapshot | None,
    *,
    max_drift_pct: Decimal,
) -> RuleResult:
    """Whether the exchange rate has moved away from the one a proposal was sized on.

    The FX analogue of :func:`reference_price_drift`, and it exists for the same
    reason: a quantity computed against a rate that no longer holds is not the
    trade the operator authorized. On a GBP account buying a USD listing, a one
    percent move in GBP/USD moves the trade's account-currency notional by one
    percent -- straight through the per-trade cap, the cash reserve and the
    concentration limit, none of which were re-derived.

    Past the envelope the proposal is **invalidated and re-derived**, never
    silently resized. Re-pricing under somebody's finger is how a person
    approves a trade they did not read.

    ``WARN`` rather than ``PASS`` when there is nothing to compare: at
    generation time no rate has been authorized yet, and reporting "no drift"
    for a comparison that never happened would be a rule that looks like it ran.
    """
    if current is None or current.same_currency:
        return RuleResult(
            rule_id="fx_rate_drift",
            rule_version=1,
            outcome=RuleOutcome.PASS,
            reason="no conversion is involved, so no rate can drift",
            observed="0",
            threshold=str(max_drift_pct),
        )
    if authorized_rate is None or authorized_rate <= ZERO:
        return RuleResult(
            rule_id="fx_rate_drift",
            rule_version=1,
            outcome=RuleOutcome.WARN,
            reason="no authorized FX rate to compare against",
            observed=None,
            threshold=str(max_drift_pct),
        )
    if not current.usable or current.rate is None:
        return RuleResult(
            rule_id="fx_rate_drift",
            rule_version=1,
            outcome=RuleOutcome.BLOCK,
            reason=(
                "the authorized FX rate cannot be compared against a current one: "
                + ("; ".join(current.blockers) or "no current rate")
            ),
            observed=None,
            threshold=str(max_drift_pct),
        )
    drift = abs(current.rate - authorized_rate) / authorized_rate
    ok = drift <= max_drift_pct
    return RuleResult(
        rule_id="fx_rate_drift",
        rule_version=1,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"{current.pair} has moved {drift:.6f} from the authorized {authorized_rate}, "
            f"within the {max_drift_pct} limit"
            if ok
            else (
                f"{current.pair} has moved {drift:.6f} from the authorized {authorized_rate}, "
                f"beyond the {max_drift_pct} limit; the account-currency size the operator "
                f"approved no longer holds"
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
