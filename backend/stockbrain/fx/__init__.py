"""Foreign exchange: typed rates, a freshness policy, and no invented numbers.

The account Phase 6 measured is GBP-denominated and every position it holds is
in another currency, so cross-currency sizing is the difference between a system
that can propose a trade and one that blocks the entire universe it can price.
This package makes that possible without ever guessing a rate: a rate carries
its provider, its timestamp and its grade, conversion is refused for any pair
that was not measured, and a missing, stale or reference-grade-but-not-permitted
rate blocks exactly as a missing quote does.
"""

from stockbrain.fx.base import (
    FxCapability,
    FxConversion,
    FxDirection,
    FxPairMismatchError,
    FxRate,
    FxRateGrade,
    FxRateProvider,
    currency_pair,
    fx_blockers,
    identity_rate,
    normalize_currency,
)
from stockbrain.fx.service import FxResolution, FxService, build_fx_provider

__all__ = [
    "FxCapability",
    "FxConversion",
    "FxDirection",
    "FxPairMismatchError",
    "FxRate",
    "FxRateGrade",
    "FxRateProvider",
    "FxResolution",
    "FxService",
    "build_fx_provider",
    "currency_pair",
    "fx_blockers",
    "identity_rate",
    "normalize_currency",
]
