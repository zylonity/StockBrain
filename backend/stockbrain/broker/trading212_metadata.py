"""Trading 212 instrument and exchange metadata -- read only.

Verified against Trading 212's current published documentation (2026-09-04,
<https://docs.trading212.com/api>):

* ``GET /api/v0/equity/metadata/instruments`` -- rate limit **1 req / 50s**,
  data refreshed every 10 minutes.  Fields: ``addedOn``, ``currencyCode``,
  ``extendedHours``, ``isin``, ``maxOpenQuantity``, ``name``, ``shortName``,
  ``ticker``, ``type``, ``workingScheduleId``.
* ``GET /api/v0/equity/metadata/exchanges`` -- rate limit **1 req / 30s**.
  Fields: ``id``, ``name``, ``workingSchedules[].id``,
  ``workingSchedules[].timeEvents[].date`` / ``.type``.
* HTTP Basic with the API key as username and the API secret as password.
* Every response carries ``x-ratelimit-limit``, ``-period``, ``-remaining``,
  ``-reset`` and ``-used``; limits are per *account*, not per key or per IP.
* Documented failure statuses for both endpoints: 401, 403, 408, 429.

Two facts drive the shape of this module:

1. **The instrument payload has no exchange field.**  It carries a
   ``workingScheduleId``, and only the exchanges endpoint says which exchange
   owns that schedule.  So both endpoints are fetched together and the exchange
   is *derived*; an instrument whose schedule is missing gets no exchange rather
   than one inferred from its ticker.
2. **There is no ``minTradeQuantity``.**  The specification assumed one.  The
   field is parsed if a future response supplies it and left NULL otherwise --
   metadata is never invented.

This module contains no order or mutation call of any kind, and it never will:
Trading 212 write access belongs to the broker adapter in a later phase, behind
the four-gate live-execution check.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stockbrain.config import Settings
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, RateLimitSnapshot, TokenBucket
from stockbrain.logging import get_logger

__all__ = [
    "EXCHANGES_PATH",
    "EXCHANGES_RATE_PERIOD_SECONDS",
    "INSTRUMENTS_PATH",
    "INSTRUMENTS_RATE_PERIOD_SECONDS",
    "T212Exchange",
    "T212Instrument",
    "T212TimeEvent",
    "T212WorkingSchedule",
    "Trading212MetadataClient",
]

log = get_logger(__name__)

INSTRUMENTS_PATH = "/equity/metadata/instruments"
EXCHANGES_PATH = "/equity/metadata/exchanges"

#: Documented per-endpoint limits, as periods in seconds for one request.
INSTRUMENTS_RATE_PERIOD_SECONDS = 50.0
EXCHANGES_RATE_PERIOD_SECONDS = 30.0


class T212TimeEvent(BaseModel):
    """One schedule boundary.  ``type`` is an open enum: unknown values are kept."""

    model_config = ConfigDict(extra="ignore")

    date: dt.datetime | None = None
    type: str | None = None


class T212WorkingSchedule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    time_events: list[T212TimeEvent] = Field(default_factory=list, alias="timeEvents")


class T212Exchange(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str | None = None
    working_schedules: list[T212WorkingSchedule] = Field(
        default_factory=list, alias="workingSchedules"
    )


class T212Instrument(BaseModel):
    """One tradable instrument, exactly as Trading 212 documents it.

    ``extra="ignore"`` so a new provider field cannot break a sync, but nothing
    the system relies on is optional-by-omission: ``ticker`` is required because
    it is the broker's identity for the instrument and there is no fallback.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str
    name: str | None = None
    short_name: str | None = Field(default=None, alias="shortName")
    isin: str | None = None
    currency_code: str | None = Field(default=None, alias="currencyCode")
    type: str | None = None
    extended_hours: bool = Field(default=False, alias="extendedHours")
    max_open_quantity: float | None = Field(default=None, alias="maxOpenQuantity")
    min_trade_quantity: float | None = Field(default=None, alias="minTradeQuantity")
    """Not in the current documented response.  Parsed if supplied, never invented."""

    working_schedule_id: int | None = Field(default=None, alias="workingScheduleId")
    added_on: dt.datetime | None = Field(default=None, alias="addedOn")


class Trading212MetadataClient:
    """Read-only metadata access to Trading 212.

    Each endpoint gets its own token bucket, because Trading 212 publishes
    per-endpoint limits an order of magnitude apart (1 req / 50s for
    instruments, 1 req / 30s for exchanges) and a shared bucket would either
    throttle the cheap call or overrun the expensive one.  The buckets live
    inside the retry loop, so a 429 retry waits for a token too.
    """

    name = "trading212_metadata"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.t212_base_url,
            headers=self._auth_headers(settings),
            timeout=httpx.Timeout(settings.t212_timeout_seconds),
            follow_redirects=True,
        )
        self._instruments_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=1.0 / INSTRUMENTS_RATE_PERIOD_SECONDS),
        )
        self._exchanges_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=1.0 / EXCHANGES_RATE_PERIOD_SECONDS),
        )
        self.last_rate_limit: RateLimitSnapshot | None = None

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """Build the documented Basic header.

        The encoded credential is constructed here and nowhere else, is never
        logged, and never appears in an exception message -- ``_classify`` in the
        shared client only ever quotes the response body, never the request.
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
    async def fetch_instruments(self) -> list[T212Instrument]:
        """Every instrument the account can trade.

        The whole list is returned in one response -- the endpoint has no
        pagination -- which is why its rate limit is one request per 50 seconds.
        """
        payload = await self._instruments_http.get_json(INSTRUMENTS_PATH, max_attempts=2)
        self.last_rate_limit = self._instruments_http.last_rate_limit
        return [
            self._parse(T212Instrument, item, "instrument")
            for item in self._expect_list(payload, INSTRUMENTS_PATH)
        ]

    async def fetch_exchanges(self) -> list[T212Exchange]:
        """Every exchange and its working schedules."""
        payload = await self._exchanges_http.get_json(EXCHANGES_PATH, max_attempts=2)
        self.last_rate_limit = self._exchanges_http.last_rate_limit
        return [
            self._parse(T212Exchange, item, "exchange")
            for item in self._expect_list(payload, EXCHANGES_PATH)
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _expect_list(payload: Any, path: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ProviderResponseError(
                f"trading212: {path} returned {type(payload).__name__}, expected a JSON array"
            )
        rows = [item for item in payload if isinstance(item, dict)]
        if len(rows) != len(payload):
            raise ProviderResponseError(
                f"trading212: {path} returned {len(payload) - len(rows)} non-object entries"
            )
        return rows

    @staticmethod
    def _parse[M: BaseModel](model: type[M], item: dict[str, Any], label: str) -> M:
        try:
            return model.model_validate(item)
        except ValidationError as exc:
            # The offending payload is not interpolated: it is provider data and
            # this message reaches logs and the API.
            raise ProviderResponseError(
                f"trading212: {label} payload did not match the documented schema "
                f"({exc.error_count()} validation error(s))"
            ) from exc

    def rate_limit_snapshot(self) -> dict[str, Any]:
        """The most recent rate-limit headers, for health reporting."""
        snapshot = self.last_rate_limit
        return snapshot.as_dict() if snapshot is not None else {}
