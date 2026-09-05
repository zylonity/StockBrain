"""Trading 212 account and position reads -- read only, and permanently so.

Verified against Trading 212's current published documentation on 2026-09-05
(<https://docs.trading212.com/api.md>):

* ``GET /api/v0/equity/account/summary`` -- rate limit **1 req / 5s**.  Returns
  ``cash.availableToTrade``, ``cash.inPies``, ``cash.reservedForOrders``,
  ``currency`` (ISO 4217, the *primary* account currency), ``id``,
  ``investments.currentValue`` / ``.realizedProfitLoss`` / ``.totalCost`` /
  ``.unrealizedProfitLoss``, and ``totalValue``.
* ``GET /api/v0/equity/positions`` -- rate limit **1 req / 1s**, optional
  ``ticker`` query parameter.  Returns ``averagePricePaid``, ``createdAt``,
  ``currentPrice``, ``instrument.{currency,isin,name,ticker}``, ``quantity``,
  ``quantityAvailableForTrading``, ``quantityInPies`` and
  ``walletImpact.{currency,currentValue,fxImpact,totalCost,unrealizedProfitLoss}``.
* Documented failure statuses for both: 401, 403, 408, 429.
* HTTP Basic, API key as username and API secret as password.

Two documented facts shape everything downstream:

1. **The old ``/equity/portfolio`` path is gone.**  Open positions are at
   ``/equity/positions`` and the payload is nested under ``instrument`` and
   ``walletImpact`` rather than flat.  A client written from the older shape
   would read ``ticker`` and ``ppl`` off the top level and find neither.
2. **Everything is reported in the primary account currency.**  Trading 212
   states that orders execute only in the primary account currency and that
   multi-currency accounts are not supported through the API, so
   ``walletImpact`` needs no conversion of ours -- and equally, StockBrain must
   not pretend it can size a listing denominated in another currency without a
   verified FX rate.

**This module has no order, amend or cancel method and must never acquire one.**
Broker mutation belongs to the execution adapter in Phase 8, behind the
four-gate live check.  ``currentPrice`` here is broker data: Trading 212's API
Terms state it is not real-time, so it is display and reconciliation only and
can never clear :func:`~stockbrain.market_data.base.quote_blockers`.
"""

from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stockbrain.config import Settings
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, RateLimitSnapshot, TokenBucket
from stockbrain.logging import get_logger

__all__ = [
    "ACCOUNT_SUMMARY_PATH",
    "POSITIONS_PATH",
    "POSITIONS_RATE_PERIOD_SECONDS",
    "SUMMARY_RATE_PERIOD_SECONDS",
    "T212AccountSummary",
    "T212Position",
    "Trading212AccountClient",
]

log = get_logger(__name__)

ACCOUNT_SUMMARY_PATH = "/equity/account/summary"
POSITIONS_PATH = "/equity/positions"

#: Documented per-endpoint limits, as periods in seconds for one request.
SUMMARY_RATE_PERIOD_SECONDS = 5.0
POSITIONS_RATE_PERIOD_SECONDS = 1.0


class T212Cash(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    available_to_trade: Decimal = Field(default=Decimal(0), alias="availableToTrade")
    in_pies: Decimal = Field(default=Decimal(0), alias="inPies")
    reserved_for_orders: Decimal = Field(default=Decimal(0), alias="reservedForOrders")


class T212Investments(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    current_value: Decimal = Field(default=Decimal(0), alias="currentValue")
    realized_profit_loss: Decimal = Field(default=Decimal(0), alias="realizedProfitLoss")
    total_cost: Decimal = Field(default=Decimal(0), alias="totalCost")
    unrealized_profit_loss: Decimal = Field(default=Decimal(0), alias="unrealizedProfitLoss")


class T212AccountSummary(BaseModel):
    """One account's cash and investment metrics, exactly as documented.

    ``currency`` and ``total_value`` are required: a snapshot without them
    cannot size anything, and defaulting either would produce a plausible-looking
    account state built on a missing field.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    currency: str
    total_value: Decimal = Field(alias="totalValue")
    cash: T212Cash = Field(default_factory=T212Cash)
    investments: T212Investments = Field(default_factory=T212Investments)


class T212Instrument(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str
    name: str | None = None
    isin: str | None = None
    currency: str | None = None


class T212WalletImpact(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    currency: str | None = None
    current_value: Decimal | None = Field(default=None, alias="currentValue")
    fx_impact: Decimal | None = Field(default=None, alias="fxImpact")
    total_cost: Decimal | None = Field(default=None, alias="totalCost")
    unrealized_profit_loss: Decimal | None = Field(default=None, alias="unrealizedProfitLoss")


class T212Position(BaseModel):
    """One open position.

    ``quantityAvailableForTrading`` is deliberately separate from ``quantity``:
    shares held inside a pie are owned but not individually tradable, so sizing
    a reduction against the total would produce an order the broker refuses.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    instrument: T212Instrument
    quantity: Decimal
    quantity_available_for_trading: Decimal = Field(
        default=Decimal(0), alias="quantityAvailableForTrading"
    )
    quantity_in_pies: Decimal = Field(default=Decimal(0), alias="quantityInPies")
    average_price_paid: Decimal | None = Field(default=None, alias="averagePricePaid")
    current_price: Decimal | None = Field(default=None, alias="currentPrice")
    """Broker-supplied and documented as **not** real-time.  Display and
    reconciliation only; never an execution reference price."""

    created_at: dt.datetime | None = Field(default=None, alias="createdAt")
    wallet_impact: T212WalletImpact = Field(default_factory=T212WalletImpact, alias="walletImpact")

    @property
    def broker_ticker(self) -> str:
        return self.instrument.ticker


class Trading212AccountClient:
    """Read-only account and position access to Trading 212.

    Each endpoint gets its own token bucket at its own documented rate (1 req/5s
    for the summary, 1 req/1s for positions) because a shared bucket would
    either throttle positions to the summary's cadence or overrun the summary.
    The buckets live inside the retry loop, so a 429 retry waits for a token too.
    """

    name = "trading212_account"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.t212_base_url,
            headers=self._auth_headers(settings),
            timeout=httpx.Timeout(settings.t212_timeout_seconds),
            follow_redirects=True,
        )
        self._summary_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=1.0 / SUMMARY_RATE_PERIOD_SECONDS),
        )
        self._positions_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=1.0 / POSITIONS_RATE_PERIOD_SECONDS),
        )
        self.last_rate_limit: RateLimitSnapshot | None = None

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """Build the documented Basic header.

        Constructed here and nowhere else, never logged, and never interpolated
        into an exception message.
        """
        pair = (
            f"{settings.t212_api_key.get_secret_value()}:"
            f"{settings.t212_api_secret.get_secret_value()}"
        )
        encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}", "Accept": "application/json"}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    async def fetch_account_summary(self) -> T212AccountSummary:
        payload = await self._summary_http.get_json(ACCOUNT_SUMMARY_PATH, max_attempts=2)
        self.last_rate_limit = self._summary_http.last_rate_limit
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                f"trading212: {ACCOUNT_SUMMARY_PATH} returned "
                f"{type(payload).__name__}, expected a JSON object"
            )
        return self._parse(T212AccountSummary, payload, "account summary")

    async def fetch_positions(self) -> list[T212Position]:
        payload = await self._positions_http.get_json(POSITIONS_PATH, max_attempts=2)
        self.last_rate_limit = self._positions_http.last_rate_limit
        if not isinstance(payload, list):
            raise ProviderResponseError(
                f"trading212: {POSITIONS_PATH} returned "
                f"{type(payload).__name__}, expected a JSON array"
            )
        rows = [item for item in payload if isinstance(item, dict)]
        if len(rows) != len(payload):
            raise ProviderResponseError(
                f"trading212: {POSITIONS_PATH} returned "
                f"{len(payload) - len(rows)} non-object entries"
            )
        return [self._parse(T212Position, item, "position") for item in rows]

    # ------------------------------------------------------------------
    @staticmethod
    def _parse[M: BaseModel](model: type[M], item: dict[str, Any], label: str) -> M:
        try:
            return model.model_validate(item)
        except ValidationError as exc:
            # The payload is provider data and this message reaches logs and the
            # API, so it is described rather than interpolated.
            raise ProviderResponseError(
                f"trading212: {label} payload did not match the documented schema "
                f"({exc.error_count()} validation error(s))"
            ) from exc

    def rate_limit_snapshot(self) -> dict[str, Any]:
        snapshot = self.last_rate_limit
        return snapshot.as_dict() if snapshot is not None else {}
