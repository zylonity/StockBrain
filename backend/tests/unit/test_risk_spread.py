"""The hard bid/ask spread ceiling.

This rule exists because of one live measurement, and the tests name it: at
03:40 UTC the "latest" Alpaca IEX quote for AAPL was the 16:00 ET closing print
with a **$33 spread on a $321.80 mid** -- a 10% round trip. The age check caught
that one. A book that wide during regular hours would be fresh and still
ruinous, so width is checked independently of age.

Every abnormal book shape is covered, because each has a different cause and
each must be refused for its own stated reason rather than collapsing into a
generic "no spread".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stockbrain.enums import SpreadStatus
from stockbrain.risk.spread import assess_spread

CEILING = Decimal("50")  # 50 bps = 0.50% of mid


def test_a_normal_book_passes_and_reports_exact_decimals() -> None:
    result = assess_spread(Decimal("100.00"), Decimal("100.10"), max_spread_bps=CEILING)
    assert result.status is SpreadStatus.OK
    assert result.is_ok
    assert result.spread == Decimal("0.10")
    assert result.mid == Decimal("100.05")
    # 0.10 / 100.05 = 0.00099950... -> 9.9950 bps
    assert result.spread_bps == Decimal("9.9950")
    assert result.spread_pct == Decimal("0.00099950")


def test_the_live_overnight_aapl_book_is_refused() -> None:
    """The observation this whole rule came from.

    bid 305.33 / ask 338.27, mid 321.80, spread 32.94 -- 1023.6 bps against a
    50 bps ceiling.
    """
    result = assess_spread(Decimal("305.33"), Decimal("338.27"), max_spread_bps=CEILING)
    assert result.status is SpreadStatus.EXCESSIVE
    assert result.mid == Decimal("321.80")
    assert result.spread == Decimal("32.94")
    assert result.spread_bps is not None and result.spread_bps > Decimal("1000")
    assert "exceeds" in result.detail
    assert result.is_merely_wide


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(None, Decimal("10")), (Decimal("10"), None), (None, None)],
)
def test_a_missing_side_is_named_missing(bid: Decimal | None, ask: Decimal | None) -> None:
    result = assess_spread(bid, ask, max_spread_bps=CEILING)
    assert result.status is SpreadStatus.MISSING
    assert not result.is_ok
    assert not result.is_merely_wide


def test_two_zero_sides_are_non_positive_not_a_zero_spread() -> None:
    """Alpaca documents ``0`` as "no active bid/ask", not a price of zero.

    Treating it as a price would produce a zero mid and a division by zero one
    refactor later.
    """
    result = assess_spread(Decimal(0), Decimal(0), max_spread_bps=CEILING)
    assert result.status is SpreadStatus.NON_POSITIVE
    assert result.mid is None


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(Decimal(0), Decimal("10")), (Decimal("10"), Decimal(0)), (Decimal("-1"), Decimal("10"))],
)
def test_one_live_side_is_one_sided(bid: Decimal, ask: Decimal) -> None:
    result = assess_spread(bid, ask, max_spread_bps=CEILING)
    assert result.status is SpreadStatus.ONE_SIDED
    assert not result.is_ok


def test_a_crossed_market_is_refused_rather_than_treated_as_free_money() -> None:
    """``ask < bid`` means the book is mid-update or mid-halt."""
    result = assess_spread(Decimal("10.50"), Decimal("10.00"), max_spread_bps=CEILING)
    assert result.status is SpreadStatus.CROSSED
    assert result.spread == Decimal("-0.50")
    assert not result.is_merely_wide, "a crossed book is not merely wide; no policy may trade it"


def test_a_locked_market_is_refused() -> None:
    """A zero-width two-sided equity quote is an artefact, not liquidity."""
    result = assess_spread(Decimal("10.00"), Decimal("10.00"), max_spread_bps=CEILING)
    assert result.status is SpreadStatus.LOCKED
    assert result.spread == Decimal(0)
    assert result.spread_bps == Decimal(0)
    assert not result.is_ok
    assert not result.is_merely_wide


def test_the_threshold_boundary_is_inclusive_and_exact() -> None:
    """A spread exactly at the ceiling passes; one cent wider does not.

    The comparison is done by cross-multiplication precisely so this boundary
    does not depend on how ``spread / mid`` happens to round.
    """
    # mid 100, ceiling 50 bps -> exactly 0.50 of spread is permitted.
    at_limit = assess_spread(Decimal("99.75"), Decimal("100.25"), max_spread_bps=CEILING)
    assert at_limit.mid == Decimal("100.00")
    assert at_limit.spread == Decimal("0.50")
    assert at_limit.status is SpreadStatus.OK

    over = assess_spread(Decimal("99.745"), Decimal("100.255"), max_spread_bps=CEILING)
    assert over.mid == Decimal("100.00")
    assert over.spread == Decimal("0.51")
    assert over.status is SpreadStatus.EXCESSIVE


def test_the_boundary_holds_where_the_ratio_does_not_terminate() -> None:
    """1/3-style ratios are exactly where a rounded comparison would drift."""
    # mid = 30, spread = 0.15 -> exactly 50 bps, but 0.15/30 = 0.005 terminates;
    # use a mid of 3 with a spread of 0.015 for the same ratio at a scale where
    # the intermediate quotient has more digits than the stored precision.
    result = assess_spread(Decimal("2.9925"), Decimal("3.0075"), max_spread_bps=CEILING)
    assert result.mid == Decimal("3.0000")
    assert result.spread == Decimal("0.0150")
    assert result.status is SpreadStatus.OK


def test_a_tighter_ceiling_rejects_what_a_looser_one_allows() -> None:
    quote = (Decimal("100.00"), Decimal("100.10"))
    assert assess_spread(*quote, max_spread_bps=Decimal("50")).status is SpreadStatus.OK
    assert assess_spread(*quote, max_spread_bps=Decimal("5")).status is SpreadStatus.EXCESSIVE


def test_no_binary_float_ever_enters_the_calculation() -> None:
    result = assess_spread(Decimal("0.1"), Decimal("0.3"), max_spread_bps=Decimal("10000"))
    assert result.spread == Decimal("0.2"), "0.3 - 0.1 must be exactly 0.2"
    assert result.mid == Decimal("0.2")
    assert isinstance(result.spread_bps, Decimal)


def test_the_assessment_serialises_every_number_as_a_string() -> None:
    """Persisted to JSONB; a float there would reintroduce the imprecision."""
    payload = assess_spread(Decimal("10"), Decimal("10.01"), max_spread_bps=CEILING).as_dict()
    for key in ("bid", "ask", "spread", "mid", "spread_pct", "spread_bps", "ceiling_bps"):
        assert isinstance(payload[key], str)
    assert payload["status"] == "OK"
