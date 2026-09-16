"""A calibration bucket is a hit rate and a mean alpha over effective grades."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from stockbrain.intelligence.memory import EffectiveGrade, calibrate

T0 = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)


def grade(correct: bool, alpha: str, hours: int = 0) -> EffectiveGrade:
    return EffectiveGrade(
        outcome_id=uuid.uuid4(),
        correct=correct,
        alpha=Decimal(alpha),
        graded_at=T0 + dt.timedelta(hours=hours),
    )


def test_an_empty_bucket_is_none() -> None:
    assert calibrate("REGULATORY×REDUCE", []) is None  # noqa: RUF001 - the key separator is literal


def test_hit_rate_and_mean_alpha_are_exact_decimals() -> None:
    bucket = calibrate(
        "REGULATORY×REDUCE",  # noqa: RUF001 - the key separator is literal
        [grade(True, "0.02"), grade(False, "-0.01"), grade(False, "-0.04", hours=5)],
    )
    assert bucket is not None
    assert bucket.key == "REGULATORY×REDUCE"  # noqa: RUF001 - the key separator is literal
    assert bucket.samples == 3
    assert bucket.correct == 1
    assert bucket.hit_rate == Decimal("0.333333")
    assert bucket.mean_alpha == Decimal("-0.01")
    assert bucket.latest_graded_at == T0 + dt.timedelta(hours=5)


def test_a_single_grade_is_a_bucket_of_one() -> None:
    bucket = calibrate("k", [grade(True, "0.1")])
    assert bucket is not None
    assert (bucket.samples, bucket.hit_rate) == (1, Decimal("1"))
