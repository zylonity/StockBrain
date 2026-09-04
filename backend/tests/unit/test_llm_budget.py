"""LLM cost estimation and budget enforcement."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.llm.base import TokenUsage
from stockbrain.llm.pricing import DEFAULT_RATES, PricingTable, is_peak

FRIDAY_PEAK = dt.datetime(2026, 9, 4, 2, 0, tzinfo=dt.UTC)
FRIDAY_OFF_PEAK = dt.datetime(2026, 9, 4, 14, 0, tzinfo=dt.UTC)
SATURDAY = dt.datetime(2026, 9, 5, 2, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Peak windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (dt.datetime(2026, 9, 4, 1, 0, tzinfo=dt.UTC), True),
        (dt.datetime(2026, 9, 4, 3, 59, tzinfo=dt.UTC), True),
        (dt.datetime(2026, 9, 4, 4, 0, tzinfo=dt.UTC), False),
        (dt.datetime(2026, 9, 4, 6, 0, tzinfo=dt.UTC), True),
        (dt.datetime(2026, 9, 4, 9, 59, tzinfo=dt.UTC), True),
        (dt.datetime(2026, 9, 4, 10, 0, tzinfo=dt.UTC), False),
        (dt.datetime(2026, 9, 4, 0, 30, tzinfo=dt.UTC), False),
        # Weekends are entirely off-peak.
        (SATURDAY, False),
        (dt.datetime(2026, 9, 6, 7, 0, tzinfo=dt.UTC), False),
    ],
)
def test_peak_windows_match_the_published_schedule(moment: dt.datetime, expected: bool) -> None:
    assert is_peak(moment) is expected


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def test_off_peak_is_exactly_half_of_peak() -> None:
    table = PricingTable()
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=1000, cache_miss_tokens=1000)
    peak = table.estimate("deepseek-v4-flash", usage, at=FRIDAY_PEAK)
    off = table.estimate("deepseek-v4-flash", usage, at=FRIDAY_OFF_PEAK)
    assert peak is not None and off is not None
    assert off == (peak / 2).quantize(Decimal("0.000001"))


def test_cache_hits_are_far_cheaper_than_misses() -> None:
    """The split is ~30x; a cost model that merges them is meaningless."""
    table = PricingTable()
    hits = TokenUsage(prompt_tokens=10_000, cache_hit_tokens=10_000, cache_miss_tokens=0)
    misses = TokenUsage(prompt_tokens=10_000, cache_hit_tokens=0, cache_miss_tokens=10_000)
    hit_cost = table.estimate("deepseek-v4-flash", hits, at=FRIDAY_PEAK)
    miss_cost = table.estimate("deepseek-v4-flash", misses, at=FRIDAY_PEAK)
    assert hit_cost is not None and miss_cost is not None
    assert miss_cost > hit_cost * 20


def test_a_million_output_tokens_costs_the_published_rate() -> None:
    table = PricingTable()
    usage = TokenUsage(completion_tokens=1_000_000)
    assert table.estimate("deepseek-v4-flash", usage, at=FRIDAY_PEAK) == Decimal("1.320000")
    assert table.estimate("deepseek-v4-pro", usage, at=FRIDAY_PEAK) == Decimal("3.960000")


def test_unpriced_models_report_none_rather_than_zero() -> None:
    """Reporting an unknown model as free would let it spend unlimited budget."""
    table = PricingTable()
    assert (
        table.estimate("some-future-model", TokenUsage(completion_tokens=999), at=FRIDAY_PEAK)
        is None
    )


def test_versioned_aliases_price_as_their_family() -> None:
    table = PricingTable()
    assert table.rates_for("deepseek-v4-flash-0731") is DEFAULT_RATES["deepseek-v4-flash"]


def test_rates_are_configurable_not_hard_wired() -> None:
    from stockbrain.llm.pricing import ModelRates

    custom = PricingTable(
        {
            "deepseek-v4-flash": ModelRates(
                cache_hit_input=Decimal("1"),
                cache_miss_input=Decimal("1"),
                output=Decimal("1"),
            )
        }
    )
    usage = TokenUsage(completion_tokens=1_000_000)
    assert custom.estimate("deepseek-v4-flash", usage, at=FRIDAY_PEAK) == Decimal("1.000000")


def test_negative_token_counts_cannot_produce_a_credit() -> None:
    table = PricingTable()
    usage = TokenUsage(prompt_tokens=-500, completion_tokens=-500, cache_miss_tokens=-500)
    cost = table.estimate("deepseek-v4-flash", usage, at=FRIDAY_PEAK)
    assert cost == Decimal("0.000000")
