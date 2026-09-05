"""Market data.

``MarketDataProvider`` is the stable internal interface; Alpaca is the first
implementation of it.  Nothing outside this package may import a provider
directly, so replacing the vendor never reaches pricing or risk logic.
"""

from stockbrain.market_data.alpaca import AlpacaMarketDataClient
from stockbrain.market_data.base import (
    Bar,
    MarketDataProvider,
    ProviderCapability,
    Quote,
    Trade,
    quote_blockers,
    to_decimal,
)
from stockbrain.market_data.reaction import PriceReaction, PriceReactionCalculator
from stockbrain.market_data.sessions import (
    SessionVerdict,
    session_from_schedule,
    session_from_us_clock,
)

__all__ = [
    "AlpacaMarketDataClient",
    "Bar",
    "MarketDataProvider",
    "PriceReaction",
    "PriceReactionCalculator",
    "ProviderCapability",
    "Quote",
    "SessionVerdict",
    "Trade",
    "quote_blockers",
    "session_from_schedule",
    "session_from_us_clock",
    "to_decimal",
]
