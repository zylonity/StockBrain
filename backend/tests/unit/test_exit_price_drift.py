"""An exit tolerates more price movement than a buy before it is cancelled."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from stockbrain.enums import OrderSide, RuleOutcome
from stockbrain.proposals.evaluation import ProposalEvaluator
from tests import risk_helpers as h


def _drift(side: OrderSide, mid: str) -> RuleOutcome:
    evaluator = ProposalEvaluator.__new__(ProposalEvaluator)
    evaluator.config = h.config(
        max_reference_price_drift_pct=Decimal("0.01"),
        max_exit_price_drift_pct=Decimal("0.05"),
    )
    proposal = SimpleNamespace(side=side, reference_price=Decimal("7.00"))
    quote = SimpleNamespace(mid=Decimal(mid))
    return evaluator._price_drift(proposal, quote).outcome  # type: ignore[arg-type]


def test_a_three_percent_move_cancels_a_buy_but_not_a_sell() -> None:
    assert _drift(OrderSide.BUY, "6.79") is RuleOutcome.BLOCK
    assert _drift(OrderSide.SELL, "6.79") is RuleOutcome.PASS


def test_a_sell_still_stops_at_its_own_limit() -> None:
    assert _drift(OrderSide.SELL, "6.50") is RuleOutcome.BLOCK
