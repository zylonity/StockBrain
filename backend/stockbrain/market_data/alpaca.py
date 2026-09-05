"""Alpaca market data: latest quote, latest trade, historical bars.

Verified against Alpaca's current OpenAPI definition (2026-09-04,
<https://docs.alpaca.markets/us/reference/stocklatestquotesingle-1>,
``stocklatesttradesingle-1``, ``stockbarsingle-1`` and the Market Data FAQ):

* ``GET https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest``
  -> ``{"symbol", "quote": {t, bx, bp, bs, ax, ap, as, c, z}}``
* ``GET /v2/stocks/{symbol}/trades/latest``
  -> ``{"symbol", "trade": {t, x, p, s, c, i, z}}``
* ``GET /v2/stocks/{symbol}/bars``
  -> ``{"symbol", "bars": [{t, o, h, l, c, v, n, vw}], "next_page_token"}``
* Auth headers ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``.
* Rate-limit headers ``X-RateLimit-Limit`` / ``-Remaining`` / ``-Reset``
  (200 req/min on the free Basic plan).

Three findings shape this module, all recorded in ``docs/sources.md``:

1. **The feed enum is wider than the specification's.** The latest endpoints
   accept ``sip``, ``iex``, ``delayed_sip``, ``otc``, ``boats`` and
   ``overnight``; the *historical* endpoints accept only
   ``iex``, ``sip``, ``otc`` and ``boats``.  ``delayed_sip`` is therefore not a
   legal bars feed, and a configuration naming it must not be forwarded blindly.
2. **The default feed depends on the account's subscription.** Documented as
   "``sip`` if the user has the unlimited subscription, otherwise ``iex``".  A
   request that omits ``feed`` therefore has a different meaning per account, so
   the feed is sent explicitly on every call -- the same lesson as DeepSeek's
   ``thinking`` parameter.
3. **HTTP 403 means both "bad credential" and "missing entitlement".** The FAQ
   documents the entitlement case as a 403 whose body reads
   ``{"code":42210000,"message":"subscription does not permit querying recent
   SIP data"}``.  The two demand opposite responses -- one is fatal, one degrades
   -- so the body is inspected to tell them apart.

Alpaca is a *data* provider here.  It never executes anything.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.enums import BarTimeframe, CapabilityState, PriceSource
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderResponseError,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.logging import get_logger
from stockbrain.market_data.base import Bar, ProviderCapability, Quote, Trade, to_decimal
from stockbrain.observability.metrics import METRICS

__all__ = [
    "ALPACA_HISTORICAL_FEEDS",
    "ALPACA_LATEST_FEEDS",
    "FEED_PRICE_SOURCE",
    "AlpacaMarketDataClient",
    "refine_alpaca_error",
]

log = get_logger(__name__)

#: Documented ``feed`` values for the latest-quote/trade endpoints.
ALPACA_LATEST_FEEDS = frozenset({"iex", "sip", "delayed_sip", "otc", "boats", "overnight"})

#: Documented ``feed`` values for the historical bars endpoint. Note the
#: absence of ``delayed_sip`` and ``overnight``: bars do not accept them.
ALPACA_HISTORICAL_FEEDS = frozenset({"iex", "sip", "otc", "boats"})

#: Which persisted price source each feed maps onto. A feed with no mapping has
#: no ``PriceSource``, and a price with no source may not be used for anything.
FEED_PRICE_SOURCE: dict[str, PriceSource] = {
    "iex": PriceSource.ALPACA_IEX,
    "sip": PriceSource.ALPACA_SIP,
    "delayed_sip": PriceSource.ALPACA_DELAYED_SIP,
}

#: Substrings Alpaca uses when the credential is fine but the plan is not.
_ENTITLEMENT_MARKERS = ("subscription does not permit", "insufficient subscription")

#: The documented error code for the entitlement case.
_ENTITLEMENT_CODE = 42210000

#: Fallback probe symbol when ``MARKET_DATA_PROBE_SYMBOL`` is blank. One request.
DEFAULT_PROBE_SYMBOL = "AAPL"


def refine_alpaca_error(response: httpx.Response, error: Exception) -> Exception:
    """Turn an entitlement 403 into a ``ProviderEntitlementError``.

    Alpaca answers 403 for a missing credential *and* for a feed outside the
    account's plan.  The shared client cannot tell them apart from the status
    alone, and the difference matters: a credential failure is fatal and must
    never be retried, while a missing entitlement degrades pricing and leaves
    ingestion, classification and resolution untouched.
    """
    if response.status_code not in (401, 403):
        return error
    try:
        body = response.json()
    except ValueError:
        return error
    if not isinstance(body, dict):
        return error
    message = str(body.get("message", ""))
    code = body.get("code")
    if code == _ENTITLEMENT_CODE or any(
        marker in message.lower() for marker in _ENTITLEMENT_MARKERS
    ):
        return ProviderEntitlementError(f"alpaca: {message or 'subscription does not permit'}")
    return error


class _AlpacaQuote(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    t: dt.datetime
    bp: float | None = None
    bs: int | None = None
    bx: str | None = None
    ap: float | None = None
    # `as` is a Python keyword; the wire name is preserved by the alias.
    ask_size: int | None = Field(default=None, alias="as")
    ax: str | None = None
    c: list[str] = Field(default_factory=list)
    z: str | None = None


class _AlpacaTrade(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: dt.datetime
    p: float
    s: int | None = None
    x: str | None = None
    c: list[str] = Field(default_factory=list)
    z: str | None = None
    i: int | None = None


class _AlpacaBar(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: dt.datetime
    o: float
    h: float
    l: float  # noqa: E741 - the wire field name is a single letter
    c: float
    v: int
    n: int | None = None
    vw: float | None = None


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


class AlpacaMarketDataClient:
    """Alpaca's REST market data, behind StockBrain's own interface."""

    name = "alpaca_market_data"

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._feed = settings.alpaca_stock_feed
        self._http = http or ProviderHttpClient(
            provider="alpaca_market_data",
            base_url=settings.alpaca_data_base_url,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
                "Accept": "application/json",
            },
            timeout_seconds=15.0,
            # 200 requests/minute on the Basic plan; stay well under it.
            rate_limiter=TokenBucket(rate_per_second=2.0, burst=5),
            refine_error=refine_alpaca_error,
        )
        self._capability = ProviderCapability(provider=self.name, feed=self._feed)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Feeds
    # ------------------------------------------------------------------
    @property
    def feed(self) -> str:
        return self._feed

    @property
    def price_source(self) -> PriceSource | None:
        return FEED_PRICE_SOURCE.get(self._feed)

    def historical_feed(self) -> str:
        """The feed to send to the *bars* endpoint.

        ``delayed_sip`` is a legal latest-quote feed but not a legal bars feed,
        so a deployment configured for it falls back to ``iex`` for history --
        recorded on every bar rather than silently substituted.
        """
        return self._feed if self._feed in ALPACA_HISTORICAL_FEEDS else "iex"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def latest_quote(self, symbol: str) -> Quote:
        ticker = symbol.strip().upper()
        payload = await self._http.get_json(
            f"/v2/stocks/{ticker}/quotes/latest", params={"feed": self._feed}
        )
        received = utcnow()
        body = self._expect_object(payload, "quote")
        quote = self._parse(_AlpacaQuote, body.get("quote"), "quote")

        source = self.price_source
        if source is None:
            raise ProviderResponseError(
                f"alpaca: feed {self._feed!r} has no price source mapping; refusing to "
                "return a price whose provenance cannot be recorded"
            )

        METRICS.inc("stockbrain_market_data_quotes_total", labels={"feed": self._feed})
        return Quote(
            symbol=str(body.get("symbol") or ticker),
            provider=self.name,
            feed=self._feed,
            price_source=source,
            provider_timestamp=_as_utc(quote.t),
            received_at=received,
            bid=to_decimal(quote.bp),
            ask=to_decimal(quote.ap),
            bid_size=quote.bs,
            ask_size=quote.ask_size,
            currency=str(body.get("currency") or "USD"),
            tape=quote.z,
            conditions=tuple(quote.c),
            entitlement=self._capability.detail,
            raw=body,
        )

    async def latest_trade(self, symbol: str) -> Trade:
        ticker = symbol.strip().upper()
        payload = await self._http.get_json(
            f"/v2/stocks/{ticker}/trades/latest", params={"feed": self._feed}
        )
        received = utcnow()
        body = self._expect_object(payload, "trade")
        trade = self._parse(_AlpacaTrade, body.get("trade"), "trade")

        source = self.price_source
        if source is None:
            raise ProviderResponseError(f"alpaca: feed {self._feed!r} has no price source mapping")
        price = to_decimal(trade.p)
        assert price is not None
        return Trade(
            symbol=str(body.get("symbol") or ticker),
            provider=self.name,
            feed=self._feed,
            price_source=source,
            price=price,
            size=trade.s,
            provider_timestamp=_as_utc(trade.t),
            received_at=received,
            exchange=trade.x,
            tape=trade.z,
            conditions=tuple(trade.c),
            raw=body,
        )

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> Sequence[Bar]:
        """Historical aggregates, oldest first, following pagination."""
        ticker = symbol.strip().upper()
        feed = self.historical_feed()
        collected: list[Bar] = []
        page_token: str | None = None
        budget = limit if limit is not None else 1000

        while len(collected) < budget:
            params: dict[str, Any] = {
                "timeframe": timeframe.value,
                "start": _iso(start),
                "end": _iso(end),
                "feed": feed,
                "sort": "asc",
                "adjustment": "raw",
                "limit": min(10000, budget - len(collected)),
            }
            if page_token:
                params["page_token"] = page_token

            payload = await self._http.get_json(f"/v2/stocks/{ticker}/bars", params=params)
            body = self._expect_object(payload, "bars")
            rows = body.get("bars")
            if rows is None:
                # Documented as required, but an empty window legitimately
                # returns null rather than []; that is no data, not a fault.
                rows = []
            if not isinstance(rows, list):
                raise ProviderResponseError("alpaca: 'bars' was not an array")

            for row in rows:
                parsed = self._parse(_AlpacaBar, row, "bar")
                collected.append(
                    Bar(
                        symbol=ticker,
                        timestamp=_as_utc(parsed.t),
                        open=_required(parsed.o),
                        high=_required(parsed.h),
                        low=_required(parsed.l),
                        close=_required(parsed.c),
                        volume=parsed.v,
                        trade_count=parsed.n,
                        vwap=to_decimal(parsed.vw),
                        feed=feed,
                    )
                )

            token = body.get("next_page_token")
            page_token = str(token) if token else None
            if not page_token or not rows:
                break

        return collected

    # ------------------------------------------------------------------
    # Capability / entitlement
    # ------------------------------------------------------------------
    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        """Actively verify what this account can reach.

        One quote request for one liquid symbol.  The result distinguishes a
        bad credential from a missing entitlement from an outage, and it never
        silently switches feed: a deployment configured for SIP that turns out
        not to own SIP is reported, not quietly downgraded, because the price
        source that ends up on a proposal must be the one that was intended.
        """
        if not refresh and self._capability.state is not CapabilityState.UNKNOWN:
            return self._capability

        checked = utcnow()
        symbol = self._settings.market_data_probe_symbol.strip().upper() or DEFAULT_PROBE_SYMBOL
        blockers: list[str] = []

        if self.price_source is None:
            self._capability = ProviderCapability(
                provider=self.name,
                state=CapabilityState.DEGRADED,
                feed=self._feed,
                detail=f"feed {self._feed!r} has no execution-grade price source mapping",
                checked_at=checked,
                realtime_pricing_usable=False,
                blockers=(f"unmapped feed {self._feed!r}",),
            )
            return self._capability

        try:
            quote = await self.latest_quote(symbol)
        except ProviderAuthError as exc:
            state, detail = CapabilityState.AUTH_FAILED, str(exc)
        except ProviderEntitlementError as exc:
            state, detail = CapabilityState.ENTITLEMENT_MISSING, str(exc)
        except ProviderError as exc:
            state, detail = CapabilityState.DOWN, f"{type(exc).__name__}: {exc}"
        else:
            usable = quote.is_two_sided and quote.price is not None
            if not usable:
                blockers.append("probe quote was not two-sided")
            state = CapabilityState.HEALTHY if usable else CapabilityState.DEGRADED

            # Freshness is reported, not folded into the state. Outside market
            # hours the latest IEX quote is the closing print and is hours old;
            # that is not an entitlement fault, and calling it one would make the
            # subsystem read DEGRADED every night. Sizing is still refused, by
            # `quote_blockers` on age.
            max_age_ms = self._settings.market_data_max_quote_age_seconds * 1000
            stale = quote.age_ms > max_age_ms
            if stale:
                blockers.append(
                    f"probe quote is {quote.age_ms}ms old, older than the "
                    f"{self._settings.market_data_max_quote_age_seconds:g}s sizing limit "
                    "(expected outside market hours)"
                )

            self._capability = ProviderCapability(
                provider=self.name,
                state=state,
                feed=self._feed,
                detail=None if usable else "probe quote carried no live bid/ask",
                checked_at=checked,
                realtime_pricing_usable=usable,
                probe_symbol=symbol,
                probe_quote_age_ms=quote.age_ms,
                probe_quote_stale=stale,
                available_feeds=(self._feed,),
                blockers=tuple(blockers),
            )
            log.info(
                "market_data_capability",
                provider=self.name,
                feed=self._feed,
                state=state.value,
                quote_age_ms=quote.age_ms,
                quote_stale=stale,
            )
            return self._capability

        blockers.append(detail[:200])
        self._capability = ProviderCapability(
            provider=self.name,
            state=state,
            feed=self._feed,
            detail=detail[:300],
            checked_at=checked,
            realtime_pricing_usable=False,
            probe_symbol=symbol,
            blockers=tuple(blockers),
        )
        log.warning(
            "market_data_capability_failed",
            provider=self.name,
            feed=self._feed,
            state=state.value,
        )
        return self._capability

    # ------------------------------------------------------------------
    @staticmethod
    def _expect_object(payload: Any, label: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                f"alpaca: {label} response was {type(payload).__name__}, expected a JSON object"
            )
        return payload

    @staticmethod
    def _parse[M: BaseModel](model: type[M], value: Any, label: str) -> M:
        if not isinstance(value, dict):
            raise ProviderResponseError(f"alpaca: {label} payload was missing or not an object")
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"alpaca: {label} payload did not match the documented schema "
                f"({exc.error_count()} validation error(s))"
            ) from exc


def _required(value: float) -> Decimal:
    """``to_decimal`` for a field the schema marks required, keeping mypy honest."""
    parsed = to_decimal(value)
    assert parsed is not None
    return parsed


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
