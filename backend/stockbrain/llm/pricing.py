"""Cost estimation for telemetry only.

Rates live in configuration, never in logic. Nothing in the trading path reads a
price: this exists so the operator can see spend and so the budget guard has a
number to compare against.

DeepSeek publishes time-of-day pricing. Verified 2026-09-04: peak is
**01:00-04:00 and 06:00-10:00 UTC, Monday to Friday**, and off-peak rates are
half the peak rates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from stockbrain.llm.base import TokenUsage

__all__ = [
    "DEFAULT_RATES",
    "ModelRates",
    "PricingTable",
    "is_peak",
]

#: Peak windows as (start_hour, end_hour) in UTC, weekdays only.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak(moment: dt.datetime) -> bool:
    """Whether ``moment`` falls in a DeepSeek peak-pricing window."""
    utc = moment.astimezone(dt.UTC)
    if utc.weekday() >= 5:  # Saturday, Sunday
        return False
    return any(start <= utc.hour < end for start, end in PEAK_WINDOWS_UTC)


@dataclass(frozen=True, slots=True)
class ModelRates:
    """USD per 1,000,000 tokens, at peak. Off-peak is applied as a multiplier."""

    cache_hit_input: Decimal
    cache_miss_input: Decimal
    output: Decimal
    off_peak_multiplier: Decimal = Decimal("0.5")


#: Published DeepSeek rates as of the verification date. Configuration may
#: override these; the application never depends on the values being current.
DEFAULT_RATES: dict[str, ModelRates] = {
    "deepseek-v4-flash": ModelRates(
        cache_hit_input=Decimal("0.014"),
        cache_miss_input=Decimal("0.44"),
        output=Decimal("1.32"),
    ),
    "deepseek-v4-pro": ModelRates(
        cache_hit_input=Decimal("0.044"),
        cache_miss_input=Decimal("1.32"),
        output=Decimal("3.96"),
    ),
    "deepseek-v4-flash-vision-exp": ModelRates(
        cache_hit_input=Decimal("0.014"),
        cache_miss_input=Decimal("0.44"),
        output=Decimal("1.32"),
    ),
}

_PER_MILLION = Decimal(1_000_000)


class PricingTable:
    """Estimates spend from token counts. Telemetry only."""

    def __init__(self, rates: dict[str, ModelRates] | None = None) -> None:
        self._rates = dict(rates or DEFAULT_RATES)

    def rates_for(self, model: str) -> ModelRates | None:
        if model in self._rates:
            return self._rates[model]
        # Aliases such as "deepseek-v4-flash-0731" should price as their family
        # rather than silently costing nothing.
        for name, rates in self._rates.items():
            if model.startswith(name):
                return rates
        return None

    def estimate(
        self, model: str, usage: TokenUsage, *, at: dt.datetime | None = None
    ) -> Decimal | None:
        """Estimated USD for one call, or ``None`` for an unpriced model.

        ``None`` is deliberate: reporting an unknown model as $0.00 would let it
        consume unlimited budget unnoticed.
        """
        rates = self.rates_for(model)
        if rates is None:
            return None

        moment = at or dt.datetime.now(dt.UTC)
        multiplier = Decimal(1) if is_peak(moment) else rates.off_peak_multiplier

        cache_hit = Decimal(max(0, usage.cache_hit_tokens))
        cache_miss = Decimal(max(0, usage.billable_cache_miss))
        output = Decimal(max(0, usage.completion_tokens))

        total = (
            cache_hit * rates.cache_hit_input
            + cache_miss * rates.cache_miss_input
            + output * rates.output
        ) / _PER_MILLION
        return (total * multiplier).quantize(Decimal("0.000001"))
