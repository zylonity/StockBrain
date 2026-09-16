"""The calibration rule only ever shrinks, and only with enough evidence."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.enums import RuleOutcome
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import CalibrationBucket
from stockbrain.risk.rules import calibration_size_factor
from tests import risk_helpers as h

ENGINE = RiskEngine()
T0 = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)


def bucket(samples: int, correct: int, mean_alpha: str = "-0.01") -> CalibrationBucket:
    return CalibrationBucket(
        key="REGULATORY×REDUCE",  # noqa: RUF001 - the key separator is literal
        samples=samples,
        correct=correct,
        hit_rate=Decimal(correct) / Decimal(samples),
        mean_alpha=Decimal(mean_alpha),
        latest_graded_at=T0,
    )


def test_no_rule_when_modulation_is_disabled() -> None:
    inputs = h.inputs(
        risk_config=h.config(calibration_modulates_size=False), calibration=bucket(20, 2)
    )
    assert calibration_size_factor(inputs) is None


def test_no_rule_without_a_bucket() -> None:
    assert calibration_size_factor(h.inputs()) is None


def test_below_the_sample_floor_the_rule_is_skipped_not_applied() -> None:
    result = calibration_size_factor(h.inputs(calibration=bucket(9, 0)))
    assert result is not None
    assert result.outcome is RuleOutcome.WARN
    assert result.size_factor is None
    assert "9 of 10" in result.reason


@pytest.mark.parametrize(
    ("correct", "expected"),
    [
        (20, Decimal("1")),  # hit rate 1.0 -> no reduction
        (10, Decimal("1")),  # 0.5 -> neutral
        (5, Decimal("0.75")),  # 0.25 -> 0.75
        (0, Decimal("0.5")),  # 0 -> floor
    ],
)
def test_the_factor_is_half_plus_hit_rate_clamped_to_the_floor(
    correct: int, expected: Decimal
) -> None:
    result = calibration_size_factor(h.inputs(calibration=bucket(20, correct)))
    assert result is not None
    assert result.rule_id == "calibration_size_modulation"
    assert result.size_factor == expected
    assert result.outcome is (RuleOutcome.PASS if expected == 1 else RuleOutcome.REDUCE)
    assert "REGULATORY×REDUCE" in result.reason  # noqa: RUF001 - literal key separator
    assert f"{correct}/20" in result.reason


def test_a_higher_floor_is_respected() -> None:
    result = calibration_size_factor(
        h.inputs(
            risk_config=h.config(min_calibration_size_factor=Decimal("0.8")),
            calibration=bucket(20, 0),
        )
    )
    assert result is not None
    assert result.size_factor == Decimal("0.8")


def test_the_rule_never_blocks_and_combines_with_the_confidence_factor() -> None:
    # A ~$20 quote is used so the reduced target still rounds to whole shares.
    # At the helper's default ~$200 ask the cap of 400 multiplied by the
    # combined factor of 0.25 rounds to zero shares, and ``allowed`` would be
    # false for a reason (whole-share rounding) that has nothing to do with the
    # rule under test.
    snapshot = h.quote(bid=Decimal("19.98"), ask=Decimal("20.02"))
    decision = ENGINE.evaluate(
        h.inputs(confidence=Decimal("0.7"), snapshot=snapshot, calibration=bucket(20, 0))
    )
    assert decision.allowed
    rule_ids = [rule.rule_id for rule in decision.rules]
    assert "calibration_size_modulation" in rule_ids
    assert "confidence_size_modulation" in rule_ids
    assert decision.blocks == ()
    baseline = ENGINE.evaluate(h.inputs(confidence=Decimal("0.7"), snapshot=snapshot))
    assert decision.sizing.quantity < baseline.sizing.quantity


def test_a_healthy_record_changes_nothing() -> None:
    with_record = ENGINE.evaluate(h.inputs(calibration=bucket(20, 15)))
    without = ENGINE.evaluate(h.inputs())
    assert with_record.sizing.quantity == without.sizing.quantity
