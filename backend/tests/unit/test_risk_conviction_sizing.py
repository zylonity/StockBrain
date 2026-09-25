"""Conviction sizing: buys track a target that moves with the account and conviction."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from stockbrain.enums import RiskOutcome, RuleOutcome
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import RiskDecision
from stockbrain.risk.rules import conviction_targets, conviction_weight
from tests import risk_helpers as h


def _config(**overrides: object) -> object:
    values: dict[str, object] = {
        "sizing_mode": "conviction",
        "target_positions": 8,
        "min_research_confidence": Decimal("0.68"),
        "conviction_full_confidence": Decimal("0.90"),
        "conviction_min_weight": Decimal("0.75"),
        "conviction_max_weight": Decimal("2.0"),
        "max_position_pct_of_portfolio": Decimal("0.30"),
        "max_aggregate_exposure_pct": Decimal("1"),
        "min_cash_reserve_pct": Decimal("0"),
        "max_active_proposal_exposure_pct": Decimal("1"),
        "min_trade_notional": Decimal("0"),
        "allow_fractional_quantity": True,
        "max_spread_bps": Decimal("100"),
        "max_notional_per_trade": Decimal("100000"),
    }
    values.update(overrides)
    return h.config(**values)


def _decide(
    confidence: str, *, cash: str = "8000", total: str = "8000", held: str | None = None
) -> RiskDecision:
    positions = {"AAPL_US_EQ": h.position(market_value=Decimal(held))} if held is not None else None
    state = h.account(
        cash=Decimal(cash),
        total=Decimal(total),
        invested=Decimal(total) - Decimal(cash),
        positions=positions,
    )
    return RiskEngine().evaluate(
        h.inputs(confidence=Decimal(confidence), risk_config=_config(), state=state)  # type: ignore[arg-type]
    )


def test_weight_runs_from_min_at_the_floor_to_max_at_full_confidence() -> None:
    config = _config()
    assert conviction_weight(config, Decimal("0.68")) == Decimal("0.75")  # type: ignore[arg-type]
    assert conviction_weight(config, Decimal("0.90")) == Decimal("2.0")  # type: ignore[arg-type]
    assert conviction_weight(config, Decimal("0.99")) == Decimal("2.0")  # type: ignore[arg-type]


def test_higher_conviction_buys_more_of_the_same_account() -> None:
    low = _decide("0.68").sizing.notional_account_currency
    high = _decide("0.90").sizing.notional_account_currency
    # 8000 / 8 = 1000 per equal share: 750 at the floor, capped at 30% (2400) not hit at 2000.
    assert Decimal("700") < low <= Decimal("750")
    assert Decimal("1950") < high <= Decimal("2000")


def test_size_scales_with_the_whole_account() -> None:
    small = _decide("0.79", cash="4000", total="4000").sizing.notional_account_currency
    large = _decide("0.79", cash="8000", total="8000").sizing.notional_account_currency
    assert abs(large - 2 * small) < Decimal("5")


def test_a_held_position_is_topped_up_to_target_not_doubled() -> None:
    decision = _decide("0.68", held="500")
    assert Decimal("200") < decision.sizing.notional_account_currency <= Decimal("250")


def test_confidence_does_not_shrink_the_target_a_second_time() -> None:
    decision = _decide("0.75")
    assert "confidence_size_modulation" not in {rule.rule_id for rule in decision.rules}


def test_a_buy_that_can_only_be_scrap_funded_is_a_capacity_block() -> None:
    # Target 1000 at weight 1; only 100 cash left -> below the 50% minimum fill.
    decision = _decide("0.73", cash="100", total="8000")
    fill = next(rule for rule in decision.rules if rule.rule_id == "target_fill")
    assert fill.outcome is RuleOutcome.BLOCK
    assert decision.outcome is RiskOutcome.BLOCK


def test_cap_mode_is_unchanged() -> None:
    decision = RiskEngine().evaluate(h.inputs(risk_config=h.config()))
    ids = {rule.rule_id for rule in decision.rules}
    assert "max_trade_pct_of_portfolio" in ids and "conviction_target" not in ids


def test_targets_split_the_whole_account_by_weight() -> None:
    config = _config(max_position_pct_of_portfolio=Decimal("1"))
    targets = conviction_targets(
        Decimal("300"),
        {"A": Decimal("2"), "B": Decimal("1")},
        config,  # type: ignore[arg-type]
    )
    assert targets == {"A": Decimal("200"), "B": Decimal("100")}


def test_a_capped_holdings_excess_is_redistributed() -> None:
    config = _config(max_position_pct_of_portfolio=Decimal("0.25"))
    targets = conviction_targets(
        Decimal("400"),
        {"A": Decimal("10"), "B": Decimal("1"), "C": Decimal("1"), "D": Decimal("1")},
        config,  # type: ignore[arg-type]
    )
    assert all(value == Decimal("100") for value in targets.values())


def test_self_adjusting_target_shrinks_as_the_book_grows() -> None:
    def notional(others: tuple[Decimal, ...]) -> Decimal:
        state = h.account(cash=Decimal("8000"), total=Decimal("8000"), invested=Decimal("0"))
        decision = RiskEngine().evaluate(
            replace(
                h.inputs(
                    confidence=Decimal("0.79"),
                    risk_config=_config(target_positions=0),  # type: ignore[arg-type]
                    state=state,
                ),
                other_holding_weights=others,
            )
        )
        return decision.sizing.notional_account_currency

    alone = notional(())
    crowded = notional((Decimal("1.375"),) * 9)
    # Alone it is capped at 30% (2400); among ten equal ideas it gets a tenth.
    assert Decimal("2350") < alone <= Decimal("2400")
    assert Decimal("750") < crowded <= Decimal("800")
