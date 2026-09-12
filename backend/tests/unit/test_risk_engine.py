"""The deterministic risk engine's rules, caps and refusals.

The engine is a pure function, so every one of these tests is an argument about
behaviour rather than about plumbing. The most important ones are the negative
ones: a block cannot be lifted, a model number cannot become a size, and a
missing fact fails closed rather than optimistically.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.enums import (
    MarketSession,
    OrderSide,
    PriceSource,
    ResolutionStatus,
    RiskOutcome,
    RuleOutcome,
    ThesisAction,
)
from stockbrain.risk.config import SpreadPolicy
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import ReservedExposure
from tests import risk_helpers as h

ENGINE = RiskEngine()


def outcome_of(inputs: object, rule_id: str) -> RuleOutcome:
    decision = ENGINE.evaluate(inputs)  # type: ignore[arg-type]
    for rule in decision.rules:
        if rule.rule_id == rule_id:
            return rule.outcome
    raise AssertionError(f"rule {rule_id} did not run")


# ---------------------------------------------------------------------------
# The happy path, so the negative tests mean something
# ---------------------------------------------------------------------------
def test_a_healthy_buy_is_allowed_and_sized() -> None:
    decision = ENGINE.evaluate(h.inputs())
    assert decision.outcome in {RiskOutcome.ALLOW, RiskOutcome.REDUCE_SIZE}
    assert decision.allowed
    assert decision.sizing.side is OrderSide.BUY
    assert decision.sizing.quantity > 0
    assert decision.blocks == ()


def test_every_rule_records_its_id_version_observed_value_and_threshold() -> None:
    """A refusal recorded as free text is a refusal nobody can audit."""
    decision = ENGINE.evaluate(h.inputs())
    assert decision.rules
    for rule in decision.rules:
        assert rule.rule_id and rule.rule_version >= 1 and rule.reason
        payload = rule.as_dict()
        assert set(payload) >= {
            "rule_id",
            "rule_version",
            "outcome",
            "reason",
            "observed",
            "threshold",
        }


def test_the_policy_version_travels_with_the_decision() -> None:
    decision = ENGINE.evaluate(h.inputs())
    assert decision.policy_version == h.config().version
    assert len(decision.snapshot_hash()) == 64


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [ResolutionStatus.AMBIGUOUS, ResolutionStatus.NOT_FOUND, ResolutionStatus.UNSUPPORTED],
)
def test_an_unresolved_identity_blocks(status: ResolutionStatus) -> None:
    """Ambiguity is a hard blocker: two plausible listings is not a coin toss."""
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(resolution_status=status)))
    assert decision.outcome is RiskOutcome.BLOCK
    assert "instrument_identity" in decision.block_rule_ids
    assert not decision.allowed


def test_a_retired_listing_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(is_active=False)))
    assert "instrument_identity" in decision.block_rule_ids


def test_a_listing_with_no_market_symbol_blocks() -> None:
    """No symbol means no price, and no price may size anything."""
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(market_symbol=None)))
    assert "instrument_identity" in decision.block_rule_ids


def test_a_listing_with_no_company_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(company_id=None)))
    assert "instrument_identity" in decision.block_rule_ids


@pytest.mark.parametrize("kind", ["WARRANT", "FOREX", "CRYPTOCURRENCY", None])
def test_an_unsupported_instrument_type_blocks(kind: str | None) -> None:
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(instrument_type=kind)))
    assert "instrument_type_supported" in decision.block_rule_ids


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def test_a_missing_quote_blocks_and_names_the_reason() -> None:
    decision = ENGINE.evaluate(
        h.inputs(snapshot=None, quote_missing_reason="market-data provider failed")
    )
    assert "quote_available" in decision.block_rule_ids
    assert any("provider failed" in reason for reason in decision.blocks)


def test_a_non_execution_grade_price_source_blocks() -> None:
    """T212 and yfinance prices can never size an order, however available."""
    decision = ENGINE.evaluate(
        h.inputs(
            snapshot=h.quote(
                price_source=PriceSource.BROKER_T212,
                provider_blockers=("price source BROKER_T212 is display/reconciliation only",),
            )
        )
    )
    assert "price_source_execution_grade" in decision.block_rule_ids


def test_a_stale_quote_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(snapshot=h.quote(age_ms=27_618_397)))
    assert "quote_freshness" in decision.block_rule_ids
    assert any("27618397ms" in reason for reason in decision.blocks)


def test_a_quote_at_exactly_the_age_limit_passes() -> None:
    assert outcome_of(h.inputs(snapshot=h.quote(age_ms=15_000)), "quote_freshness") is (
        RuleOutcome.PASS
    )
    assert outcome_of(h.inputs(snapshot=h.quote(age_ms=15_001)), "quote_freshness") is (
        RuleOutcome.BLOCK
    )


def test_a_fresh_but_excessively_wide_quote_blocks_by_default() -> None:
    """The Phase 6 requirement, stated as a test.

    The live overnight AAPL book: fresh enough to pass an age check in regular
    hours, and a 10% round trip.
    """
    decision = ENGINE.evaluate(
        h.inputs(snapshot=h.quote(bid=Decimal("305.33"), ask=Decimal("338.27"), age_ms=100))
    )
    assert decision.outcome is RiskOutcome.BLOCK
    assert "spread_ceiling" in decision.block_rule_ids
    assert outcome_of(h.inputs(snapshot=h.quote(age_ms=100)), "quote_freshness") is RuleOutcome.PASS


def test_a_wide_quote_reduces_instead_of_blocking_under_an_explicit_policy() -> None:
    """``REDUCE`` is an explicit configuration choice, never a silent fallback."""
    config = h.config(
        spread_policy=SpreadPolicy.REDUCE,
        wide_spread_size_factor=Decimal("0.25"),
        max_notional_per_trade=Decimal("10000"),
        max_trade_pct_of_portfolio=Decimal("0.5"),
        max_position_pct_of_portfolio=Decimal("0.5"),
    )
    decision = ENGINE.evaluate(
        h.inputs(
            risk_config=config,
            confidence=Decimal("1"),
            snapshot=h.quote(bid=Decimal("199.00"), ask=Decimal("201.00")),
        )
    )
    assert decision.outcome is RiskOutcome.REDUCE_SIZE
    assert decision.allowed
    spread_rule = next(rule for rule in decision.rules if rule.rule_id == "spread_ceiling")
    assert spread_rule.outcome is RuleOutcome.REDUCE
    assert spread_rule.size_factor == Decimal("0.25")


@pytest.mark.parametrize(
    ("bid", "ask"),
    [
        (Decimal("10.50"), Decimal("10.00")),  # crossed
        (Decimal("10.00"), Decimal("10.00")),  # locked
        (Decimal("10.00"), Decimal(0)),  # one-sided
    ],
)
def test_a_malformed_book_blocks_even_under_the_reduce_policy(bid: Decimal, ask: Decimal) -> None:
    """Crossed, locked and one-sided are not "wide" -- they have no usable mid."""
    config = h.config(spread_policy=SpreadPolicy.REDUCE)
    decision = ENGINE.evaluate(h.inputs(risk_config=config, snapshot=h.quote(bid=bid, ask=ask)))
    assert decision.outcome is RiskOutcome.BLOCK


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session",
    [MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS, MarketSession.CLOSED],
)
def test_a_session_outside_the_allowed_set_blocks(session: MarketSession) -> None:
    decision = ENGINE.evaluate(h.inputs(snapshot=h.quote(session=session)))
    assert "market_session" in decision.block_rule_ids


def test_an_unknown_session_blocks_rather_than_being_assumed_tradable() -> None:
    """No schedule covered the instant; that is not evidence the market is open."""
    decision = ENGINE.evaluate(
        h.inputs(snapshot=h.quote(session=MarketSession.UNKNOWN, session_source="none"))
    )
    assert "market_session" in decision.block_rule_ids


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------
def test_missing_account_state_fails_closed() -> None:
    decision = ENGINE.evaluate(
        h.inputs(state=None, account_state_missing_reason="no snapshot has ever been taken")
    )
    assert decision.outcome is RiskOutcome.BLOCK
    assert "account_state_available" in decision.block_rule_ids
    assert any("no snapshot has ever been taken" in reason for reason in decision.blocks)


def test_stale_account_state_fails_closed() -> None:
    stale = h.account(captured_at=h.NOW - dt.timedelta(minutes=30))
    decision = ENGINE.evaluate(h.inputs(state=stale))
    assert "account_state_freshness" in decision.block_rule_ids


def test_account_state_at_exactly_the_age_limit_passes() -> None:
    at_limit = h.account(captured_at=h.NOW - dt.timedelta(seconds=300))
    assert outcome_of(h.inputs(state=at_limit), "account_state_freshness") is RuleOutcome.PASS
    over = h.account(captured_at=h.NOW - dt.timedelta(seconds=301))
    assert outcome_of(h.inputs(state=over), "account_state_freshness") is RuleOutcome.BLOCK


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
def test_cross_currency_sizing_is_refused_rather_than_invented() -> None:
    """With ``require_same_currency`` set, a mismatch is refused and says why.

    Phase 9 split the question in two -- ``currency_alignment`` is the policy
    ("may these differ?") and ``fx_available`` is the capability ("is there a
    verified rate?"). This asserts the policy half still blocks and still names
    the setting responsible, so a reader of the refusal knows which knob to
    turn rather than guessing.
    """
    # The real shape of the problem: a GBP account and a USD listing, priced in
    # USD. Exactly what Phase 6 measured on 14 of 14 live positions.
    decision = ENGINE.evaluate(
        h.inputs(
            instrument=h.identity(currency="USD"),
            state=h.account(currency="GBP"),
            snapshot=h.quote(currency="USD"),
        )
    )
    assert "currency_alignment" in decision.block_rule_ids
    assert any("RISK_REQUIRE_SAME_CURRENCY" in reason for reason in decision.blocks)
    assert not decision.sizing.executable
    assert decision.sizing.quantity == 0


def test_a_quote_in_another_currency_blocks_even_when_the_instrument_matches() -> None:
    decision = ENGINE.evaluate(h.inputs(snapshot=h.quote(currency="EUR")))
    assert "currency_alignment" in decision.block_rule_ids


def test_an_unknown_currency_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(instrument=h.identity(currency=None)))
    assert "currency_alignment" in decision.block_rule_ids


# ---------------------------------------------------------------------------
# Position and action semantics
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", [ThesisAction.SELL, ThesisAction.REDUCE])
def test_selling_without_a_position_blocks_because_shorting_is_disabled(
    action: ThesisAction,
) -> None:
    decision = ENGINE.evaluate(h.inputs(action=action))
    assert "current_position" in decision.block_rule_ids
    assert any("short selling is disabled" in reason for reason in decision.blocks)


def test_sell_closes_the_whole_available_position() -> None:
    state = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("7"))})
    decision = ENGINE.evaluate(h.inputs(action=ThesisAction.SELL, state=state))
    assert decision.allowed
    assert decision.sizing.side is OrderSide.SELL
    assert decision.sizing.quantity == Decimal("7")
    assert decision.sizing.reference_price == Decimal("199.95"), "a sell hits the bid"


def test_reduce_is_a_deterministic_partial_exit_not_a_liquidation() -> None:
    state = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("10"))})
    decision = ENGINE.evaluate(h.inputs(action=ThesisAction.REDUCE, state=state))
    assert decision.sizing.quantity == Decimal("5")
    assert decision.sizing.quantity < Decimal("10")


def test_reduce_respects_the_quantity_available_for_trading_not_the_total_held() -> None:
    """Shares inside a pie are owned but not individually tradable."""
    state = h.account(
        positions={
            "AAPL_US_EQ": h.position(quantity=Decimal("10"), available=Decimal("4")),
        }
    )
    decision = ENGINE.evaluate(h.inputs(action=ThesisAction.REDUCE, state=state))
    assert decision.sizing.quantity == Decimal("2")


@pytest.mark.parametrize("action", [ThesisAction.HOLD, ThesisAction.NO_ACTION])
def test_hold_and_no_action_produce_no_executable_order(action: ThesisAction) -> None:
    decision = ENGINE.evaluate(h.inputs(action=action))
    assert decision.outcome is RiskOutcome.BLOCK
    assert "action_is_executable" in decision.block_rule_ids
    assert decision.sizing.side is None
    assert decision.sizing.quantity == 0
    assert not decision.allowed


def test_exposure_caps_do_not_apply_to_a_risk_reducing_action() -> None:
    """Limits bound risk taken, not risk removed.

    A cap that can stop a position from being closed is a hazard, not a control.
    """
    broke = h.account(
        cash=Decimal(0),
        invested=Decimal("100000"),
        total=Decimal("100000"),
        positions={"AAPL_US_EQ": h.position(quantity=Decimal("5"))},
    )
    assert ENGINE.evaluate(h.inputs(action=ThesisAction.SELL, state=broke)).allowed
    assert not ENGINE.evaluate(h.inputs(action=ThesisAction.BUY, state=broke)).allowed


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
def test_the_smallest_cap_decides_the_size() -> None:
    config = h.config(
        max_notional_per_trade=Decimal("300"), max_trade_pct_of_portfolio=Decimal("1")
    )
    decision = ENGINE.evaluate(h.inputs(risk_config=config, confidence=Decimal("1")))
    # 300 / 200.05 rounds down to one whole share.
    assert decision.sizing.quantity == Decimal("1")
    assert decision.sizing.max_notional == Decimal("300")


def test_a_percentage_cap_binds_when_it_is_tighter_than_the_absolute_one() -> None:
    config = h.config(
        max_notional_per_trade=Decimal("100000"),
        max_trade_pct_of_portfolio=Decimal("0.01"),
    )
    decision = ENGINE.evaluate(h.inputs(risk_config=config, confidence=Decimal("1")))
    assert decision.sizing.max_notional == Decimal("200"), "1% of a 20,000 portfolio"


def test_an_existing_position_consumes_concentration_headroom() -> None:
    """Already at the concentration cap means there is nothing left to buy."""
    state = h.account(
        positions={"AAPL_US_EQ": h.position(quantity=Decimal("5"), market_value=Decimal("600"))}
    )
    decision = ENGINE.evaluate(h.inputs(state=state, confidence=Decimal("1")))
    assert decision.outcome is RiskOutcome.BLOCK
    assert "max_position_concentration" in decision.block_rule_ids


def test_aggregate_exposure_blocks_when_the_book_is_already_full() -> None:
    state = h.account(invested=Decimal("18000"), total=Decimal("20000"))
    decision = ENGINE.evaluate(h.inputs(state=state))
    assert "max_aggregate_exposure" in decision.block_rule_ids


def test_the_cash_reserve_is_honoured() -> None:
    """10% of a 20,000 portfolio must stay uncommitted."""
    state = h.account(cash=Decimal("2000"), total=Decimal("20000"))
    decision = ENGINE.evaluate(h.inputs(state=state))
    assert "min_cash_reserve" in decision.block_rule_ids


def test_reserved_exposure_from_other_proposals_reduces_the_budget() -> None:
    """Two proposals must not each assume the whole headroom is theirs."""
    reserved = ReservedExposure(count=1, notional=Decimal("1900"))
    decision = ENGINE.evaluate(h.inputs(reserved=reserved, confidence=Decimal("1")))
    cash_rule = next(rule for rule in decision.rules if rule.rule_id == "active_proposal_exposure")
    assert cash_rule.max_notional == Decimal("100")
    assert cash_rule.outcome is RuleOutcome.REDUCE


def test_the_broker_max_open_quantity_caps_the_size() -> None:
    """``maxOpenQuantity`` is a verified broker constraint, so it is honoured.

    (There is deliberately no minimum: Phase 4 measured ``minTradeQuantity``
    populated on 0 of 17,452 live instruments, so nothing invents one.)
    """
    state = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("9"))})
    decision = ENGINE.evaluate(
        h.inputs(
            instrument=h.identity(max_open_quantity=Decimal("10")),
            state=state,
            risk_config=h.config(
                max_position_pct_of_portfolio=Decimal("1"),
                max_notional_per_trade=Decimal("100000"),
                max_trade_pct_of_portfolio=Decimal("1"),
            ),
            confidence=Decimal("1"),
        )
    )
    broker_rule = next(
        rule for rule in decision.rules if rule.rule_id == "broker_max_open_quantity"
    )
    assert broker_rule.max_notional == Decimal("200.00"), "one remaining share at the 200 mid"
    assert decision.sizing.max_quantity <= Decimal("1")


def test_a_conflicting_live_proposal_for_the_same_listing_blocks() -> None:
    reserved = ReservedExposure(
        count=1,
        notional=Decimal("100"),
        same_instrument_count=1,
        same_instrument_notional=Decimal("100"),
        same_instrument_sides=("SELL",),
    )
    decision = ENGINE.evaluate(h.inputs(reserved=reserved))
    assert "duplicate_or_conflicting_proposal" in decision.block_rule_ids


def test_the_active_proposal_count_limit_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(reserved=ReservedExposure(count=5)))
    assert "max_active_proposals" in decision.block_rule_ids


# ---------------------------------------------------------------------------
# Research confidence: bounded, and never authoritative
# ---------------------------------------------------------------------------
def test_confidence_below_the_floor_blocks() -> None:
    decision = ENGINE.evaluate(h.inputs(confidence=Decimal("0.4")))
    assert "research_confidence_floor" in decision.block_rule_ids


def test_confidence_only_ever_shrinks_a_size() -> None:
    high = ENGINE.evaluate(h.inputs(confidence=Decimal("1.0")))
    low = ENGINE.evaluate(h.inputs(confidence=Decimal("0.7")))
    assert low.sizing.target_notional <= high.sizing.target_notional
    factor = next(
        rule for rule in low.rules if rule.rule_id == "confidence_size_modulation"
    ).size_factor
    assert factor is not None and Decimal("0.5") <= factor <= Decimal(1)


def test_maximum_confidence_cannot_exceed_the_deterministic_cap() -> None:
    """Confidence is a ranking feature, not a licence to exceed a limit."""
    config = h.config(max_notional_per_trade=Decimal("400"))
    decision = ENGINE.evaluate(h.inputs(risk_config=config, confidence=Decimal("1")))
    assert decision.sizing.target_notional <= Decimal("400")
    assert decision.sizing.max_notional == Decimal("400")


def test_perfect_confidence_cannot_lift_a_single_block() -> None:
    """The system's central claim, as one assertion."""
    decision = ENGINE.evaluate(
        h.inputs(
            confidence=Decimal("1.0"),
            instrument=h.identity(resolution_status=ResolutionStatus.AMBIGUOUS),
            snapshot=h.quote(age_ms=999_999, bid=Decimal("1"), ask=Decimal("99")),
            state=None,
        )
    )
    assert decision.outcome is RiskOutcome.BLOCK
    assert not decision.allowed
    assert decision.sizing.quantity == 0
    assert decision.sizing.side is None


def test_confidence_modulation_can_be_switched_off() -> None:
    config = h.config(confidence_modulates_size=False)
    decision = ENGINE.evaluate(h.inputs(risk_config=config, confidence=Decimal("0.75")))
    assert all(rule.rule_id != "confidence_size_modulation" for rule in decision.rules)


@pytest.mark.parametrize("action", [ThesisAction.SELL, ThesisAction.REDUCE])
def test_low_confidence_never_blocks_a_risk_reducing_action(action: ThesisAction) -> None:
    """Refusing to close a position because the research was lukewarm is the
    hazard the rules module's own header names."""
    assert (
        outcome_of(
            h.inputs(
                action=action,
                confidence=Decimal("0.10"),
                state=h.account(positions={"AAPL_US_EQ": h.position()}),
            ),
            "research_confidence_floor",
        )
        is not RuleOutcome.BLOCK
    )


def test_low_confidence_still_blocks_a_buy() -> None:
    assert (
        outcome_of(
            h.inputs(action=ThesisAction.BUY, confidence=Decimal("0.10")),
            "research_confidence_floor",
        )
        is RuleOutcome.BLOCK
    )


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------
def test_a_blocked_decision_carries_no_size_at_all() -> None:
    """Not "a size that is ignored" -- no size. One refactor's worth of safety."""
    decision = ENGINE.evaluate(h.inputs(confidence=Decimal("0.1")))
    assert decision.sizing.quantity == 0
    assert decision.sizing.target_notional == 0
    assert decision.sizing.reference_price is None
    assert decision.sizing.side is None
    assert not decision.sizing.executable


def test_a_rule_that_could_not_run_is_recorded_as_a_warning_not_a_pass() -> None:
    decision = ENGINE.evaluate(h.inputs(snapshot=None, quote_missing_reason="provider down"))
    skipped = next(rule for rule in decision.rules if rule.rule_id == "quote_freshness")
    assert skipped.outcome is RuleOutcome.WARN
    assert skipped.reason.startswith("not evaluated")


def test_the_decision_serialises_to_json_ready_primitives() -> None:
    import json

    payload = ENGINE.evaluate(h.inputs()).as_dict()
    json.dumps(payload)  # raises if a Decimal or datetime leaked through
    assert payload["allowed"] is True
    assert payload["sizing"]["side"] == "BUY"


def test_the_snapshot_hash_changes_when_any_input_changes() -> None:
    base = ENGINE.evaluate(h.inputs(), now=h.NOW).snapshot_hash()
    same = ENGINE.evaluate(h.inputs(), now=h.NOW).snapshot_hash()
    different = ENGINE.evaluate(h.inputs(confidence=Decimal("0.8")), now=h.NOW).snapshot_hash()
    assert base == same
    assert base != different
