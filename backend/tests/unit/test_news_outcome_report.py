"""Offline report helpers: headline typing, sparse suppression, deadline exits."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from stockbrain.intelligence.news_outcome_report import (
    _distribution,
    _relative_at_session_close,
    headline_type,
)
from tests.unit.test_news_outcome_validation import BASE, _bar, _event


def test_headline_type_is_a_labelled_keyword_rule() -> None:
    assert headline_type("Apple Q3 earnings beat estimates") == "EARNINGS"
    assert headline_type("Morgan Stanley downgrade on Intel") == "ANALYST"
    assert headline_type("Walgreens agrees to take-private buyout") == "M_AND_A"
    assert headline_type("Something else entirely") == "OTHER"
    assert headline_type(None) == "OTHER"


def test_a_sparse_distribution_reports_counts_but_no_statistics() -> None:
    values = [Decimal("0.01"), Decimal("-0.02"), Decimal("0")]
    result = _distribution(values, unavailable=4)
    assert (result["positive"], result["flat"], result["negative"]) == (1, 1, 1)
    assert result["unavailable"] == 4
    assert result["sparse"] is True
    assert result["p_positive"] is None and result["median_net"] is None


def test_a_full_distribution_includes_losses_and_an_interval() -> None:
    values = [Decimal("0.01")] * 6 + [Decimal("-0.01")] * 4
    result = _distribution(values, unavailable=0)
    assert result["p_positive"] == "0.600"
    low, high = result["p_positive_95ci"]
    assert Decimal(low) < Decimal("0.6") < Decimal(high)


def test_relative_move_at_a_later_session_close_uses_aligned_bars() -> None:
    item = _event("a")
    later = BASE + dt.timedelta(days=1)
    close = later.replace(hour=19, minute=59)
    stock = (*item.stock_bars, _bar("AAPL", close, "103"))
    market = (*item.benchmark_bars, _bar("SPY", close, "101"))
    shifted = type(item)(
        item.canonical_id,
        item.symbol,
        item.event_type,
        item.published_at,
        item.live_received_at,
        item.outcome,
        stock,
        market,
    )
    assert _relative_at_session_close(shifted, 1) == Decimal("0.02")
    assert _relative_at_session_close(shifted, 2) is None


def test_same_session_stories_do_not_count_as_independent_evidence() -> None:
    """Twenty stories entering on two sessions are two observations, not twenty."""
    result = _distribution([Decimal("0.01")] * 20, unavailable=0, sessions=2)
    assert result["usable"] == 20 and result["distinct_entry_sessions"] == 2
    assert result["sparse"] is True and result["median_net"] is None
