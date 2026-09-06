"""Cost estimation for telemetry and the spend guard.

Rates live in configuration, never in logic. Nothing in the trading path reads a
price: this exists so the operator can see spend and so the budget guard has a
number to compare against.

Two rules survive the move to arbitrary providers, and they are the reason this
module is not simply a dictionary:

* **An unpriced model estimates ``None``, never ``0``.** Reporting an unknown
  model as free would let it consume an unlimited budget unnoticed, because the
  guard sums estimates. ``None`` is what makes the configuration requirement in
  :mod:`stockbrain.config` enforceable rather than advisory.
* **Unreported cache hits bill at the uncached rate.** Over-estimating spend
  trips a cap early; under-estimating spends past it. Only the first is
  recoverable, so no provider is ever credited with a cache hit it did not
  report.

Built-in rates exist only for DeepSeek, StockBrain's default backend. Every
other provider supplies its rates through configuration, because a hard spend
limit must not depend on a constant that was accurate when this file was
written. ``python -m stockbrain.llm.rates_cli`` helps populate those values; it
is an offline authoring aid and is deliberately not reachable from the runtime
path.

DeepSeek publishes time-of-day pricing. Verified 2026-09-04: peak is
**01:00-04:00 and 06:00-10:00 UTC, Monday to Friday**, and off-peak rates are
half the peak rates. Providers with flat pricing carry a multiplier of 1 and
never consult the clock.
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

#: Peak windows as (start_hour, end_hour) in UTC, weekdays only. DeepSeek-specific;
#: consulted only for rates that carry an off-peak multiplier other than 1.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak(moment: dt.datetime) -> bool:
    """Whether ``moment`` falls in a DeepSeek peak-pricing window."""
    utc = moment.astimezone(dt.UTC)
    if utc.weekday() >= 5:  # Saturday, Sunday
        return False
    return any(start <= utc.hour < end for start, end in PEAK_WINDOWS_UTC)


@dataclass(frozen=True, slots=True)
class ModelRates:
    """USD per 1,000,000 tokens.

    ``off_peak_multiplier`` defaults to 1: flat pricing is the norm, and a
    provider that happens to discount at night must say so explicitly rather
    than inherit a discount from an unrelated provider's schedule.
    """

    cache_hit_input: Decimal
    cache_miss_input: Decimal
    output: Decimal
    off_peak_multiplier: Decimal = Decimal("1")


#: Published DeepSeek rates as of the verification date. Configuration may
#: override these; the application never depends on the values being current.
DEFAULT_RATES: dict[str, ModelRates] = {
    "deepseek-v4-flash": ModelRates(
        cache_hit_input=Decimal("0.014"),
        cache_miss_input=Decimal("0.44"),
        output=Decimal("1.32"),
        off_peak_multiplier=Decimal("0.5"),
    ),
    "deepseek-v4-pro": ModelRates(
        cache_hit_input=Decimal("0.044"),
        cache_miss_input=Decimal("1.32"),
        output=Decimal("3.96"),
        off_peak_multiplier=Decimal("0.5"),
    ),
    "deepseek-v4-flash-vision-exp": ModelRates(
        cache_hit_input=Decimal("0.014"),
        cache_miss_input=Decimal("0.44"),
        output=Decimal("1.32"),
        off_peak_multiplier=Decimal("0.5"),
    ),
}

_PER_MILLION = Decimal(1_000_000)


class PricingTable:
    """Estimates spend from token counts."""

    def __init__(self, rates: dict[str, ModelRates] | None = None) -> None:
        self._rates = dict(rates or DEFAULT_RATES)

    @classmethod
    def from_settings(cls, settings: object) -> PricingTable:
        """Build a table for the configured provider.

        The built-in DeepSeek rates are always present so the default install
        keeps working untouched. Configured rates are then registered under the
        configured model names, which lets them both add a new provider and
        correct a stale built-in without editing code.
        """
        rates = dict(DEFAULT_RATES)
        configured = getattr(settings, "llm_model_rates", None)
        if callable(configured):
            rates.update(configured())
        return cls(rates)

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

        multiplier = Decimal(1)
        if rates.off_peak_multiplier != 1:
            moment = at or dt.datetime.now(dt.UTC)
            if not is_peak(moment):
                multiplier = rates.off_peak_multiplier

        cache_hit = Decimal(max(0, usage.cache_hit_tokens))
        cache_miss = Decimal(max(0, usage.billable_cache_miss))
        output = Decimal(max(0, usage.completion_tokens))

        total = (
            cache_hit * rates.cache_hit_input
            + cache_miss * rates.cache_miss_input
            + output * rates.output
        ) / _PER_MILLION
        return (total * multiplier).quantize(Decimal("0.000001"))
