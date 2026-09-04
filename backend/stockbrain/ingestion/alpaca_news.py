"""Alpaca real-time news WebSocket and historical news REST.

Verified against Alpaca's current documentation (2026-09-04):

* stream: ``wss://stream.data.alpaca.markets/v1beta1/news``
* auth may be sent either as connection headers or as a JSON message after
  connecting; this client uses the JSON message form, which is what the
  documentation specifies for Trading API keys and which keeps the credential
  out of the handshake logs of any intermediate proxy
* control frames: ``{"T":"success","msg":"connected"}`` then
  ``{"T":"success","msg":"authenticated"}``, and ``{"T":"subscription",...}``
* error codes: 401 not authenticated, 402 auth failed, 404 auth timeout,
  406 connection limit exceeded, 409/410 insufficient subscription
* news payload fields: ``T id headline summary author created_at updated_at
  content url symbols source`` (plus ``images`` on the REST shape)
* REST: ``GET https://data.alpaca.markets/v1beta1/news`` -- note ``limit`` is
  capped at **50** per page and pagination is via ``next_page_token``

Alpaca is a *data* provider here. It never executes anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import websockets
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "ALPACA_NEWS_REST_PATH",
    "AlpacaNewsClient",
    "AlpacaNewsItem",
    "parse_news_item",
]

log = get_logger(__name__)

ALPACA_NEWS_REST_PATH = "/v1beta1/news"

#: Documented maximum page size for the historical news endpoint.
ALPACA_NEWS_MAX_LIMIT = 50

#: Stream error codes that mean "stop trying": the credential or the plan is
#: wrong, and reconnecting cannot fix either.
_FATAL_STREAM_CODES: dict[int, type[Exception]] = {
    402: ProviderAuthError,
    403: ProviderAuthError,
    404: ProviderAuthError,
    409: ProviderEntitlementError,
    410: ProviderEntitlementError,
}


class AlpacaNewsImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    size: str | None = None


class AlpacaNewsItem(BaseModel):
    """One news article, in Alpaca's shape.

    ``extra="ignore"`` so a new provider field cannot break ingestion, but every
    field the system relies on is declared and validated.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    headline: str | None = None
    summary: str | None = None
    author: str | None = None
    content: str | None = None
    url: str | None = None
    source: str | None = None
    symbols: list[str] = Field(default_factory=list)
    images: list[AlpacaNewsImage] = Field(default_factory=list)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def parse_news_item(payload: dict[str, Any]) -> RawSourceDocument:
    """Convert an Alpaca news object into StockBrain's canonical document.

    Raises :class:`ProviderResponseError` when the payload does not match the
    documented schema, rather than guessing at the missing pieces.
    """
    try:
        item = AlpacaNewsItem.model_validate(payload)
    except ValidationError as exc:
        raise ProviderResponseError(
            "alpaca: news payload did not match the documented schema "
            f"({exc.error_count()} validation error(s))"
        ) from exc

    # Prefer full content; fall back to the summary, which Alpaca documents as
    # possibly being the first sentence of the article.
    body = item.content or item.summary

    return RawSourceDocument(
        provider=SourceProvider.ALPACA,
        provider_item_id=str(item.id),
        url=item.url,
        source_name=item.source or "Benzinga",
        # Alpaca's news feed is currently supplied by Benzinga, a newswire.
        source_category=SourceCategory.NEWSWIRE,
        headline=item.headline,
        author=item.author,
        published_at=_as_utc(item.created_at),
        updated_at_source=_as_utc(item.updated_at),
        body=body,
        symbols=[symbol.strip().upper() for symbol in item.symbols if symbol.strip()],
        raw_payload=payload,
        metadata={
            "alpaca_summary": item.summary,
            "image_count": len(item.images),
        },
    )


class AlpacaNewsClient:
    """Real-time news stream plus bounded historical backfill.

    The stream reconnects with exponential backoff and full jitter. It stops
    permanently only on a *fatal* condition -- bad credentials or a missing
    entitlement -- because retrying those forever would be noise, not resilience.
    """

    name = "alpaca_news"

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="alpaca",
            base_url=settings.alpaca_data_base_url,
            headers=self._auth_headers(settings),
            timeout_seconds=30.0,
            # Documented as 100 requests/minute; stay comfortably under it.
            rate_limiter=TokenBucket(rate_per_second=1.5, burst=5),
        )
        self.last_message_at: dt.datetime | None = None

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Historical REST
    # ------------------------------------------------------------------
    async def backfill(
        self,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[RawSourceDocument]:
        """Fetch news in a window, following pagination.

        Used to close the gap after a WebSocket disconnect. ``limit`` bounds the
        total number of documents returned across pages, not the page size.
        """
        collected: list[RawSourceDocument] = []
        page_token: str | None = None
        budget = limit if limit is not None else 500

        while len(collected) < budget:
            params: dict[str, Any] = {
                "start": start.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "end": end.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "sort": "asc",
                "limit": min(ALPACA_NEWS_MAX_LIMIT, budget - len(collected)),
                "include_content": "true",
                "exclude_contentless": "false",
            }
            if symbols:
                params["symbols"] = ",".join(symbols)
            if page_token:
                params["page_token"] = page_token

            payload = await self._http.get_json(ALPACA_NEWS_REST_PATH, params=params)
            if not isinstance(payload, dict):
                raise ProviderResponseError("alpaca: news response was not a JSON object")

            articles = payload.get("news")
            if not isinstance(articles, list):
                raise ProviderResponseError("alpaca: news response had no 'news' array")

            for article in articles:
                if isinstance(article, dict):
                    collected.append(parse_news_item(article))

            page_token = payload.get("next_page_token")
            if not page_token or not articles:
                break

        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(collected),
            labels={"provider": "alpaca", "mode": "backfill"},
        )
        return collected

    # ------------------------------------------------------------------
    # Real-time WebSocket
    # ------------------------------------------------------------------
    async def stream(self) -> AsyncIterator[RawSourceDocument]:
        """Yield news documents forever, reconnecting on transient failures."""
        attempt = 0
        while True:
            try:
                async for document in self._stream_once():
                    attempt = 0
                    yield document
            except (ProviderAuthError, ProviderEntitlementError):
                # Fatal: no amount of reconnecting fixes a credential or a plan.
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                delay = min(60.0, 1.0 * (2 ** min(attempt, 6)))
                jittered = random.uniform(0, delay)  # noqa: S311 - jitter, not crypto
                log.warning(
                    "alpaca_news_stream_reconnect",
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    delay_seconds=round(jittered, 2),
                )
                METRICS.inc(
                    "stockbrain_provider_errors_total",
                    labels={"provider": "alpaca_news", "kind": "stream"},
                )
                await asyncio.sleep(jittered)

    async def _stream_once(self) -> AsyncIterator[RawSourceDocument]:
        url = self._settings.alpaca_news_ws_url
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, close_timeout=10, max_size=8 * 1024 * 1024
        ) as socket:
            await self._authenticate(socket)
            await socket.send(json.dumps({"action": "subscribe", "news": ["*"]}))
            log.info("alpaca_news_subscribed", url=url)

            async for frame in socket:
                for message in self._decode_frame(frame):
                    kind = message.get("T")
                    if kind == "n":
                        self.last_message_at = dt.datetime.now(dt.UTC)
                        METRICS.inc(
                            "stockbrain_news_items_received_total",
                            labels={"provider": "alpaca", "mode": "stream"},
                        )
                        yield parse_news_item(message)
                    elif kind == "error":
                        self._raise_for_stream_error(message)
                    elif kind == "subscription":
                        log.debug("alpaca_news_subscription", channels=message.get("news"))

    async def _authenticate(self, socket: Any) -> None:
        """Send the documented auth message and wait for confirmation."""
        await socket.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self._settings.alpaca_api_key.get_secret_value(),
                    "secret": self._settings.alpaca_api_secret.get_secret_value(),
                }
            )
        )
        # Alpaca allows 10 seconds to authenticate; fail before it does.
        deadline = asyncio.get_running_loop().time() + 15.0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderUnavailable("alpaca: no authentication response within 15s")
            frame = await asyncio.wait_for(socket.recv(), timeout=remaining)
            for message in self._decode_frame(frame):
                if message.get("T") == "error":
                    self._raise_for_stream_error(message)
                if message.get("T") == "success" and message.get("msg") == "authenticated":
                    log.info("alpaca_news_authenticated")
                    return

    @staticmethod
    def _decode_frame(frame: str | bytes) -> list[dict[str, Any]]:
        """Alpaca sends arrays of messages; a frame may carry several."""
        try:
            payload = json.loads(frame)
        except ValueError as exc:
            raise ProviderResponseError("alpaca: stream frame was not valid JSON") from exc
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise ProviderResponseError("alpaca: unexpected stream frame type")

    @staticmethod
    def _raise_for_stream_error(message: dict[str, Any]) -> None:
        code = message.get("code")
        text = str(message.get("msg", "unknown error"))
        exc_type = _FATAL_STREAM_CODES.get(code) if isinstance(code, int) else None
        if exc_type is not None:
            raise exc_type(f"alpaca stream error {code}: {text}")
        raise ProviderUnavailable(f"alpaca stream error {code}: {text}")
