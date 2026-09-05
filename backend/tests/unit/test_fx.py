"""Foreign-exchange rates: direction, inversion, freshness and grade.

The invariant every test here protects is one sentence: **a wrong exchange rate
is a wrong position size, and it is silent.** A quote that is stale by an hour
announces itself in the spread; a rate that is inverted produces a plausible
number and commits 1.8x the intended cash.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.fx.base import (
    FxDirection,
    FxPairMismatchError,
    FxRate,
    FxRateGrade,
    currency_pair,
    fx_blockers,
    identity_rate,
    normalize_currency,
)

NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)

_EXECUTION_LIMIT = Decimal("900")
_REFERENCE_LIMIT = Decimal("90000")


def rate(
    *,
    base: str = "GBP",
    quote: str = "USD",
    value: Decimal = Decimal("1.35"),
    grade: FxRateGrade = FxRateGrade.EXECUTION,
    age_seconds: int = 5,
    provider: str = "test",
) -> FxRate:
    return FxRate(
        base_currency=base,
        quote_currency=quote,
        rate=value,
        provider=provider,
        grade=grade,
        provider_timestamp=NOW - dt.timedelta(seconds=age_seconds),
        received_at=NOW,
    )


def blockers(measured: FxRate | None, **overrides: object) -> list[str]:
    kwargs: dict[str, object] = {
        "from_currency": "GBP",
        "to_currency": "USD",
        "now": NOW,
        "max_age_seconds": _EXECUTION_LIMIT,
        "reference_max_age_seconds": _REFERENCE_LIMIT,
        "allow_reference_grade": False,
    }
    kwargs.update(overrides)
    return fx_blockers(measured, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_a_rate_must_be_positive() -> None:
    """Zero or negative is a parsing failure, not a market condition.

    Checked in ``__post_init__`` rather than at each call site: a provider
    adapter that mis-reads a payload must not be able to hand a plausible
    ``Decimal`` to sizing.
    """
    for bad in (Decimal(0), Decimal("-1.35")):
        with pytest.raises(ValueError, match="must be positive"):
            rate(value=bad)


def test_a_rate_cannot_have_the_same_currency_twice() -> None:
    """``GBP/GBP`` is not a measurement, and 1.0 is not a rate.

    The type refuses to represent it, which is what makes "silently used 1.0
    for a cross-currency trade" unreachable rather than merely unlikely.
    """
    with pytest.raises(ValueError, match="two different currencies"):
        rate(base="GBP", quote="GBP")


def test_currency_codes_are_normalised_before_comparison() -> None:
    """``"usd"`` and ``"USD "`` are the same currency.

    Three providers with three conventions; comparing them raw is how
    ``"usd" != "USD"`` becomes a blocked trade nobody can explain.
    """
    assert normalize_currency(" usd ") == "USD"
    assert normalize_currency(None) == ""
    assert currency_pair("gbp", "usd") == "GBPUSD"
    assert rate().supports("gbp", "usd")


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def test_a_direct_conversion_multiplies() -> None:
    """A GBP/USD rate of 1.35 turns 500 GBP into 675 USD, exactly."""
    conversion = rate().convert(Decimal("500"), "GBP", "USD", now=NOW)
    assert conversion.converted == Decimal("675.00")
    assert conversion.direction is FxDirection.DIRECT
    assert conversion.rate == Decimal("1.35")


def test_an_inverted_conversion_divides() -> None:
    """The same rate turns 675 USD back into 500 GBP.

    Inversion is arithmetic on the pair that *was* measured, so it introduces
    no source and no assumption -- which is why it is the one derivation this
    module permits.
    """
    conversion = rate().convert(Decimal("675"), "USD", "GBP", now=NOW)
    assert conversion.converted == Decimal("500")
    assert conversion.direction is FxDirection.INVERTED


def test_converting_a_pair_that_was_not_measured_is_refused() -> None:
    """No chaining through a third currency, ever.

    Two published rates multiplied together produce a number no source
    published and no counterparty will honour, and the error compounds
    silently.
    """
    with pytest.raises(FxPairMismatchError, match="never chains rates"):
        rate().convert(Decimal("100"), "EUR", "JPY", now=NOW)
    with pytest.raises(FxPairMismatchError):
        rate().convert(Decimal("100"), "GBP", "EUR", now=NOW)


def test_a_round_trip_returns_the_original_amount() -> None:
    """GBP -> USD -> GBP is the identity, to the precision Decimal keeps.

    A float implementation of the same two steps does not satisfy this, which
    is the entire reason money is Decimal here.
    """
    measured = rate(value=Decimal("1.3521"))
    forward = measured.convert(Decimal("500"), "GBP", "USD", now=NOW)
    back = measured.convert(forward.converted, "USD", "GBP", now=NOW)
    assert back.converted == Decimal("500")


def test_the_identity_conversion_is_its_own_state() -> None:
    """Same-currency is reported as ``IDENTITY``, not as a rate of one.

    A reader has to be able to tell "no conversion was needed" from "converted
    at parity"; only one of those is a claim about a market.
    """
    conversion = identity_rate("GBP", now=NOW)
    assert conversion.direction is FxDirection.IDENTITY
    assert conversion.provider == "identity"
    assert conversion.rate == Decimal(1)
    assert conversion.age_seconds == Decimal(0)


def test_a_conversion_carries_everything_needed_to_recheck_it() -> None:
    """Persisted onto a proposal, so the size stays auditable afterwards."""
    payload = rate().convert(Decimal("500"), "GBP", "USD", now=NOW).as_dict()
    for key in (
        "amount",
        "converted",
        "rate",
        "direction",
        "provider",
        "pair",
        "grade",
        "provider_timestamp",
        "received_at",
        "age_seconds",
    ):
        assert key in payload, key


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------
def test_a_future_timestamp_reports_zero_age_not_a_negative_one() -> None:
    """A provider clock slightly ahead is not a rate from the future.

    A negative age would sail through every freshness comparison, which is the
    worst possible direction for the error to run in.
    """
    ahead = rate(age_seconds=-30)
    assert ahead.age_seconds(NOW) == Decimal(0)


# ---------------------------------------------------------------------------
# The blocker predicate
# ---------------------------------------------------------------------------
def test_same_currency_needs_no_rate_and_reports_no_blocker() -> None:
    """The only case where a missing rate is not a blocker."""
    assert blockers(None, from_currency="GBP", to_currency="GBP") == []


def test_a_missing_rate_blocks_with_a_reason() -> None:
    assert any("no FX rate is available" in reason for reason in blockers(None))


def test_a_supplied_missing_reason_wins_over_the_generic_one() -> None:
    """The caller knows *why* the rate is missing; the predicate does not.

    "The subscription does not include forex" and "the API timed out" demand
    different operator responses, and collapsing them into "no rate" loses the
    only useful part.
    """
    reasons = blockers(None, missing_reason="the plan does not include forex")
    assert reasons == ["the plan does not include forex"]


def test_an_unknown_currency_blocks() -> None:
    assert blockers(rate(), to_currency="") != []


def test_the_wrong_pair_blocks_and_short_circuits() -> None:
    """One reason, not four: every later check would be about the wrong pair."""
    reasons = blockers(rate(base="EUR", quote="JPY"))
    assert len(reasons) == 1
    assert "cannot convert GBP to USD" in reasons[0]


def test_a_stale_execution_grade_rate_blocks() -> None:
    reasons = blockers(rate(age_seconds=3600))
    assert any("older than the 900s limit" in reason for reason in reasons)


def test_a_reference_grade_rate_is_refused_unless_explicitly_permitted() -> None:
    """Frankfurter's own documentation says it "is not for live trading".

    Honouring that is the difference between using a published fixing knowingly
    and mistaking it for a dealable quote.
    """
    fixing = rate(grade=FxRateGrade.REFERENCE, age_seconds=3600)
    refused = blockers(fixing)
    assert any("reference-grade fixing" in reason for reason in refused)
    # Permitted, and judged against the *reference* age budget -- an hour is
    # stale for a live feed and perfectly ordinary for a daily fixing.
    assert blockers(fixing, allow_reference_grade=True) == []


def test_a_reference_grade_rate_still_has_an_age_limit() -> None:
    """Permitting a fixing is not permitting an arbitrarily old one.

    Twenty-five hours covers one publication gap. A weekend needs three days
    and blocks, which is correct: equity markets are closed then anyway.
    """
    weekend = rate(grade=FxRateGrade.REFERENCE, age_seconds=3 * 86400)
    reasons = blockers(weekend, allow_reference_grade=True)
    assert any("older than the 90000s limit" in reason for reason in reasons)


def test_a_day_precision_timestamp_is_floored_to_over_state_the_age() -> None:
    """A date-only publication is aged from the start of that day.

    Over-stating an age can only make a freshness check stricter, which is the
    safe direction. Under-stating it would let a three-day-old fixing look
    fresh on the third morning.
    """
    published_today = FxRate(
        base_currency="GBP",
        quote_currency="USD",
        rate=Decimal("1.3521"),
        provider="frankfurter",
        grade=FxRateGrade.REFERENCE,
        provider_timestamp=NOW.replace(hour=0, minute=0),
        received_at=NOW,
        provider_timestamp_precision="day",
    )
    assert published_today.age_seconds(NOW) == Decimal("43200.0")
