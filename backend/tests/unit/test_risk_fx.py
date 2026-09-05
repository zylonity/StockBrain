"""Cross-currency risk gating and sizing.

Phase 6 measured the live account as GBP with **14 of 14** positions in another
currency, so ``currency_alignment`` blocked the entire universe StockBrain could
price.  Phase 9 lifts that block *only* behind a verified rate.  These tests are
the boundary of that permission.

The arithmetic being protected: a cap of 500 GBP against a 200 USD ask at
GBP/USD 1.35 is ``500 * 1.35 / 200 = 3.375`` -> **3 shares**, committing 600 USD
= 444.44 GBP.  Getting the direction wrong gives ``500 / 1.35 / 200 = 1.85`` ->
1 share, or -- worse, if the caps are converted the other way -- 6 shares and
889 GBP of a 500 GBP allowance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from stockbrain.enums import RuleOutcome, ThesisAction
from stockbrain.fx.base import FxDirection, FxRateGrade
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import (
    FxSnapshot,
    RiskDecision,
    RiskInputs,
    RuleResult,
)
from stockbrain.risk.rules import fx_rate_drift
from tests import risk_helpers as h

ENGINE = RiskEngine()

#: Cross-currency sizing permitted by policy. Both switches, because
#: `RISK_REQUIRE_SAME_CURRENCY=false` on its own is refused at startup.
_PERMISSIVE = h.config(require_same_currency=False)


def cross(**overrides: Any) -> RiskInputs:
    """A GBP account, a USD listing, priced in USD.

    The real shape of the problem: Phase 6 measured it on 14 of 14 live
    positions. A function rather than a dict of keyword arguments so the
    argument types survive into the call.
    """
    values: dict[str, Any] = {
        "instrument": h.identity(currency="USD"),
        "state": h.account(currency="GBP"),
        "snapshot": h.quote(currency="USD"),
    }
    values.update(overrides)
    return h.inputs(**values)


def _rule(decision: RiskDecision, rule_id: str) -> RuleResult:
    matching = [rule for rule in decision.rules if rule.rule_id == rule_id]
    assert matching, f"{rule_id} did not run at all"
    return matching[0]


# ---------------------------------------------------------------------------
# The policy gate
# ---------------------------------------------------------------------------
def test_the_policy_gate_and_the_capability_gate_are_separate_rules() -> None:
    """``currency_alignment`` asks "may they differ"; ``fx_available`` asks "is
    there a rate".

    Phase 6 conflated them, and the permitted branch returned ``WARN`` -- so a
    deployment that set ``RISK_REQUIRE_SAME_CURRENCY=false`` got a warning
    nobody had to act on and a quantity computed by dividing a GBP ceiling by a
    USD ask. Splitting them is what makes each answer checkable.
    """
    decision = ENGINE.evaluate(cross())
    assert _rule(decision, "currency_alignment").outcome is RuleOutcome.BLOCK
    assert _rule(decision, "fx_available").outcome is RuleOutcome.BLOCK


def test_permitting_a_mismatch_without_a_rate_still_blocks() -> None:
    """The configuration switch does not conjure a rate.

    This is the regression test for the Phase 6 hazard: the policy now passes
    and the capability gate is what refuses, so no path exists in which a
    mismatch is permitted *and* unpriced.
    """
    decision = ENGINE.evaluate(cross(risk_config=_PERMISSIVE, fx=None))
    assert _rule(decision, "currency_alignment").outcome is RuleOutcome.PASS
    assert _rule(decision, "fx_available").outcome is RuleOutcome.BLOCK
    assert not decision.sizing.executable
    assert decision.sizing.quantity == 0


def test_a_quote_in_a_third_currency_blocks_whatever_the_policy_says() -> None:
    """No single rate reconciles three currencies.

    A USD listing quoted in EUR is a resolution failure, and it blocks even in
    the permissive configuration with a perfectly good GBP/USD rate on hand.
    """
    decision = ENGINE.evaluate(
        h.inputs(
            instrument=h.identity(currency="USD"),
            state=h.account(currency="GBP"),
            snapshot=h.quote(currency="EUR"),
            risk_config=_PERMISSIVE,
            fx=h.fx_snapshot(),
        )
    )
    rule = _rule(decision, "currency_alignment")
    assert rule.outcome is RuleOutcome.BLOCK
    assert "three currencies" in rule.reason


# ---------------------------------------------------------------------------
# The capability gates
# ---------------------------------------------------------------------------
def test_a_same_currency_trade_passes_both_fx_gates_without_a_rate() -> None:
    """Nothing to convert, so nothing to be missing or stale."""
    decision = ENGINE.evaluate(h.inputs())
    assert _rule(decision, "fx_available").outcome is RuleOutcome.PASS
    assert _rule(decision, "fx_freshness").outcome is RuleOutcome.PASS
    assert decision.allowed


def test_a_stale_rate_blocks_freshness_and_not_availability() -> None:
    """The split matters at send time.

    ``stockbrain.execution.preflight`` treats a *missing* input as transient
    (the proposal survives a provider outage) and a *stale* one as a deferral.
    Reporting a stale rate as "unavailable" would retire authorized proposals
    every time a feed fell behind.
    """
    stale = h.fx_snapshot(
        blockers=(
            "the GBPUSD rate is 4000.0s old, older than the 900s limit for a "
            "execution-grade source",
        ),
        age_seconds=Decimal("4000"),
    )
    decision = ENGINE.evaluate(cross(risk_config=_PERMISSIVE, fx=stale))
    assert _rule(decision, "fx_available").outcome is RuleOutcome.PASS
    assert _rule(decision, "fx_freshness").outcome is RuleOutcome.BLOCK


def test_an_untrusted_grade_blocks_availability_and_not_freshness() -> None:
    """A reference fixing that the operator has not permitted is unavailable.

    It is not stale -- it may be minutes old -- it is simply not a source this
    deployment has agreed to size against.
    """
    fixing = h.fx_snapshot(
        grade=FxRateGrade.REFERENCE,
        blockers=(
            "the frankfurter rate is a reference-grade fixing, not a dealable quote; "
            "set FX_ALLOW_REFERENCE_GRADE=true to size against it knowingly",
        ),
    )
    decision = ENGINE.evaluate(cross(risk_config=_PERMISSIVE, fx=fixing))
    assert _rule(decision, "fx_available").outcome is RuleOutcome.BLOCK
    assert _rule(decision, "fx_freshness").outcome is RuleOutcome.PASS


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def test_a_gbp_account_sizes_a_usd_listing_through_the_verified_rate() -> None:
    """The arithmetic from the module docstring, asserted exactly.

    The cap moves to the price, never the price to the cap: ``max_notional`` is
    in GBP, the ask is in USD, and dividing one by the other directly is the
    operation Phase 6 refused to perform.
    """
    decision = ENGINE.evaluate(
        cross(
            risk_config=h.config(
                require_same_currency=False,
                max_notional_per_trade=Decimal("500"),
                max_trade_pct_of_portfolio=Decimal("1"),
                min_trade_notional=Decimal("20"),
                confidence_modulates_size=False,
            ),
            fx=h.fx_snapshot(rate=Decimal("1.35")),
        )
    )
    assert decision.allowed
    sizing = decision.sizing
    # 500 GBP -> 675 USD; 675 / 200.05 (the ask) = 3.37 -> 3 whole shares.
    assert sizing.quantity == Decimal(3)
    assert sizing.currency == "USD"
    assert sizing.account_currency == "GBP"
    assert sizing.max_notional == Decimal("500")
    assert sizing.max_notional_instrument_currency == Decimal("675.00")
    # 3 x 200.05 = 600.15 USD, which is 444.55... GBP.
    assert sizing.target_notional == Decimal("600.15")
    assert sizing.notional_account_currency == (Decimal("600.15") / Decimal("1.35"))
    # And the account-currency cost is inside the account-currency cap, which is
    # the property the whole conversion exists to preserve.
    assert sizing.notional_account_currency <= sizing.max_notional


def test_the_inverted_pair_produces_the_same_size() -> None:
    """A provider publishing USD/GBP instead of GBP/USD changes nothing.

    Market convention decides which way a pair is quoted, and inversion is
    exact. If these two disagreed, the size would depend on the provider's
    formatting preference.
    """
    config = h.config(
        require_same_currency=False,
        max_notional_per_trade=Decimal("500"),
        max_trade_pct_of_portfolio=Decimal("1"),
        confidence_modulates_size=False,
    )
    for value in (Decimal("1.35"), Decimal("1.25")):
        direct = ENGINE.evaluate(cross(risk_config=config, fx=h.fx_snapshot(rate=value)))
        inverted = ENGINE.evaluate(
            cross(risk_config=config, fx=h.fx_snapshot(rate=value, invert=True))
        )
        assert direct.sizing.quantity == inverted.sizing.quantity > 0

    # 1.25 has an exact reciprocal, so the account-currency cost is identical to
    # the last digit and can be asserted without a tolerance. For a rate whose
    # reciprocal does not terminate the two differ far below any cap -- the
    # published pair is genuinely a different finite number from its inverse,
    # and pretending otherwise would be the assertion that is wrong.
    exact_direct = ENGINE.evaluate(
        cross(risk_config=config, fx=h.fx_snapshot(rate=Decimal("1.25")))
    )
    exact_inverted = ENGINE.evaluate(
        cross(risk_config=config, fx=h.fx_snapshot(rate=Decimal("1.25"), invert=True))
    )
    assert (
        exact_direct.sizing.notional_account_currency
        == exact_inverted.sizing.notional_account_currency
    )


def test_the_snapshot_records_which_direction_the_conversion_used() -> None:
    """``DIRECT`` and ``INVERTED`` are both correct and are not the same fact.

    Persisted onto the proposal so a reader checking the arithmetic afterwards
    knows whether to multiply or divide.
    """
    assert h.fx_snapshot().direction() is FxDirection.DIRECT
    assert h.fx_snapshot(invert=True).direction() is FxDirection.INVERTED
    assert FxSnapshot.same_currency_snapshot("GBP").direction() is FxDirection.IDENTITY


def test_the_minimum_trade_notional_is_compared_in_the_account_currency() -> None:
    """Every ``RISK_*`` money limit is denominated in the account's currency.

    Comparing a USD notional against a GBP floor would let a trade through at
    roughly three quarters of the intended minimum, or refuse one at four
    thirds of it, depending on the pair.
    """
    decision = ENGINE.evaluate(
        cross(
            risk_config=h.config(
                require_same_currency=False,
                # 250 GBP -> 337.50 USD -> 1 share at 200.05 = 200.05 USD
                # = 148.19 GBP, which is below a 200 GBP floor and above a
                # 200 USD one. Only the account-currency reading refuses.
                max_notional_per_trade=Decimal("250"),
                max_trade_pct_of_portfolio=Decimal("1"),
                min_trade_notional=Decimal("200"),
                confidence_modulates_size=False,
            ),
            fx=h.fx_snapshot(rate=Decimal("1.35")),
        )
    )
    assert not decision.sizing.executable
    assert any(
        "below the 200 minimum trade notional" in reason for reason in decision.sizing.reasons
    )


def test_a_sell_reports_its_proceeds_in_both_currencies() -> None:
    """A reduction removes exposure, so no cap applies -- but the proceeds still
    have to be readable in the currency the portfolio is measured in."""
    decision = ENGINE.evaluate(
        h.inputs(
            action=ThesisAction.SELL,
            instrument=h.identity(currency="USD"),
            state=h.account(
                currency="GBP",
                positions={"AAPL_US_EQ": h.position(quantity=Decimal(10), available=Decimal(10))},
            ),
            snapshot=h.quote(currency="USD"),
            risk_config=_PERMISSIVE,
            fx=h.fx_snapshot(rate=Decimal("1.35")),
        )
    )
    sizing = decision.sizing
    assert sizing.quantity == Decimal(10)
    assert sizing.target_notional == Decimal(10) * Decimal("199.95")
    assert sizing.notional_account_currency == sizing.target_notional / Decimal("1.35")


def test_an_unusable_snapshot_never_produces_a_size() -> None:
    """Defence in depth for a caller that bypassed the gates.

    Reported as a non-executable result rather than raised: a sizing function
    that can throw is a sizing function that can take down the proposal sweep.
    """
    unusable = h.fx_snapshot(blockers=("no rate",))
    decision = ENGINE.evaluate(cross(risk_config=_PERMISSIVE, fx=unusable))
    assert not decision.sizing.executable
    assert decision.sizing.quantity == 0


def test_the_broker_quantity_cap_is_converted_before_it_is_compared() -> None:
    """The one cap derived from a price rather than from a portfolio figure.

    ``remaining_quantity * mid`` is an instrument-currency number sitting in a
    list of account-currency ceilings. Unconverted on a GBP/USD pair it is 35%
    too small and silently becomes the binding cap.
    """
    from stockbrain.risk.rules import notional_caps

    caps = {
        cap.rule_id: cap
        for cap in notional_caps(
            h.inputs(
                instrument=h.identity(currency="USD", max_open_quantity=Decimal("10")),
                state=h.account(currency="GBP"),
                snapshot=h.quote(currency="USD"),
                risk_config=_PERMISSIVE,
                fx=h.fx_snapshot(rate=Decimal("1.35")),
            )
        )
    }
    cap = caps["broker_max_open_quantity"]
    # 10 shares at a 200.00 mid = 2000 USD = 1481.48... GBP.
    assert cap.limit == (Decimal("10") * Decimal("200.00") / Decimal("1.35"))
    assert cap.rule_version == 2


def test_the_cap_is_omitted_rather_than_guessed_when_no_rate_is_usable() -> None:
    """A converted ceiling nobody can derive is not a ceiling.

    The decision is blocked by ``fx_available`` regardless; the point is that
    no invented number reaches the cap list on the way there.
    """
    from stockbrain.risk.rules import notional_caps

    caps = {
        cap.rule_id
        for cap in notional_caps(
            h.inputs(
                instrument=h.identity(currency="USD", max_open_quantity=Decimal("10")),
                state=h.account(currency="GBP"),
                snapshot=h.quote(currency="USD"),
                risk_config=_PERMISSIVE,
                fx=h.fx_snapshot(blockers=("no rate",)),
            )
        )
    }
    assert "broker_max_open_quantity" not in caps


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------
def test_no_conversion_means_no_drift() -> None:
    result = fx_rate_drift(
        None, FxSnapshot.same_currency_snapshot("GBP"), max_drift_pct=Decimal("0.005")
    )
    assert result.outcome is RuleOutcome.PASS


def test_a_missing_authorized_rate_warns_rather_than_passing() -> None:
    """At generation time nothing has been authorized yet.

    Reporting ``PASS`` for a comparison that never happened would be a rule
    that looks like it ran.
    """
    result = fx_rate_drift(None, h.fx_snapshot(), max_drift_pct=Decimal("0.005"))
    assert result.outcome is RuleOutcome.WARN


def test_a_rate_inside_the_envelope_passes() -> None:
    result = fx_rate_drift(
        Decimal("1.3500"), h.fx_snapshot(rate=Decimal("1.3540")), max_drift_pct=Decimal("0.005")
    )
    assert result.outcome is RuleOutcome.PASS


def test_a_rate_past_the_envelope_blocks_so_the_proposal_is_re_derived() -> None:
    """On a GBP account buying USD, a one percent move in the pair moves the
    account-currency notional by one percent -- straight through the per-trade
    cap, the cash reserve and the concentration limit, none of which were
    re-derived. Past the envelope the proposal is invalidated, never resized."""
    result = fx_rate_drift(
        Decimal("1.3500"), h.fx_snapshot(rate=Decimal("1.3900")), max_drift_pct=Decimal("0.005")
    )
    assert result.outcome is RuleOutcome.BLOCK
    assert "no longer holds" in result.reason


def test_drift_cannot_be_judged_against_an_unusable_current_rate() -> None:
    """Missing "now" is not evidence of no movement."""
    result = fx_rate_drift(
        Decimal("1.3500"),
        h.fx_snapshot(blockers=("the source is down",)),
        max_drift_pct=Decimal("0.005"),
    )
    assert result.outcome is RuleOutcome.BLOCK


def test_drift_is_symmetric() -> None:
    """A pair that strengthened and one that weakened by the same fraction are
    the same amount of "the trade is no longer what was approved"."""
    up = fx_rate_drift(
        Decimal("1.35"), h.fx_snapshot(rate=Decimal("1.40")), max_drift_pct=Decimal("0.005")
    )
    down = fx_rate_drift(
        Decimal("1.35"), h.fx_snapshot(rate=Decimal("1.30")), max_drift_pct=Decimal("0.005")
    )
    assert up.outcome is down.outcome is RuleOutcome.BLOCK
