"""Explicit research providers: FRED macro and Phase 4 Alpaca context only."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from stockbrain.db.base import utcnow
from stockbrain.enums import BarTimeframe
from stockbrain.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.intelligence.research import ResearchDatum, ResearchPacket
from stockbrain.market_data.base import Bar, MarketDataProvider, ProviderCapability, Quote, Trade
from stockbrain.market_data.reaction import PriceReactionCalculator


class Observation(BaseModel):
    date: dt.date
    value: str
    realtime_start: dt.date
    realtime_end: dt.date


class Observations(BaseModel):
    observations: list[Observation] = Field(max_length=100)


def fred_error(response: httpx.Response, error: Exception) -> Exception:
    if response.status_code == 400 and "api_key" in response.text.lower():
        return ProviderAuthError("fred: API key rejected")
    if response.status_code == 423:
        return ProviderUnavailable("fred: resource locked")
    if isinstance(error, ProviderResponseError):
        return ProviderResponseError(f"fred: invalid response (HTTP {response.status_code})")
    return error


class FredMacroProvider:
    # DFF measures policy rates; DGS10 measures long-term rates. No catalogue scan.
    ALLOWED_SERIES = frozenset({"DFF", "DGS10"})

    def __init__(
        self,
        key: SecretStr,
        *,
        series: tuple[str, ...] = ("DFF", "DGS10"),
        http: ProviderHttpClient | None = None,
    ) -> None:
        if not set(series) <= self.ALLOWED_SERIES or not series:
            raise ValueError("unsupported FRED research series")
        self.key = key
        self.series = series
        self.http = http or ProviderHttpClient(
            provider="fred",
            base_url="https://api.stlouisfed.org/fred/",
            rate_limiter=TokenBucket(1, burst=1),
            max_attempts=2,
            refine_error=fred_error,
        )

    async def context(self, as_of: dt.datetime) -> tuple[ResearchDatum, ...]:
        if not self.key.get_secret_value():
            raise ProviderAuthError("fred: API key missing")
        # FRED's vintage granularity is a day. Use the prior Chicago day so an
        # intraday replay cannot see revisions released later on the analysis day.
        vintage = min(as_of, utcnow()).astimezone(
            ZoneInfo("America/Chicago")
        ).date() - dt.timedelta(days=1)
        start = vintage - dt.timedelta(days=90)
        results = []
        for series_id in self.series:
            payload = await self.http.get_json(
                "series/observations",
                params={
                    "api_key": self.key.get_secret_value(),
                    "series_id": series_id,
                    "file_type": "json",
                    "observation_start": start.isoformat(),
                    "observation_end": vintage.isoformat(),
                    "realtime_start": vintage.isoformat(),
                    "realtime_end": vintage.isoformat(),
                    "units": "lin",
                    "sort_order": "desc",
                    "limit": 100,
                },
            )
            try:
                parsed = Observations.model_validate(payload)
                values = []
                for row in parsed.observations:
                    if (
                        not start <= row.date <= vintage
                        or not row.realtime_start <= vintage <= row.realtime_end
                    ):
                        raise ValueError("observation outside vintage")
                    value = None if row.value == "." else Decimal(row.value)
                    if value is not None and not value.is_finite():
                        raise ValueError("non-finite observation")
                    values.append(
                        {
                            "date": row.date.isoformat(),
                            "value": str(value) if value is not None else None,
                        }
                    )
            except (ValidationError, ValueError, InvalidOperation):
                raise ProviderResponseError("fred: malformed observations") from None
            results.append(
                ResearchDatum(
                    provider="fred",
                    kind=series_id,
                    as_of=as_of,
                    text=json.dumps(
                        {
                            "series_id": series_id,
                            "vintage": vintage.isoformat(),
                            "observations": values,
                        }
                    ),
                )
            )
        return tuple(results)

    async def aclose(self) -> None:
        await self.http.aclose()


class AlpacaResearchProvider:
    """Research snapshot from the existing market provider, without an SDK fallback."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
        bars = await self.provider.bars(
            packet.company.symbol,
            BarTimeframe.DAY_1,
            packet.as_of - dt.timedelta(days=365),
            packet.as_of,
            limit=260,
        )
        # Daily bars are timestamped at their opening. Exclude the analysis day
        # entirely, since an in-progress daily close is not historical evidence.
        eligible = sorted(
            (bar for bar in bars if bar.timestamp + dt.timedelta(days=1) <= packet.as_of),
            key=lambda bar: bar.timestamp,
        )
        if not eligible:
            raise ProviderResponseError("alpaca research: no completed historical bars")
        eligible = eligible[-180:]
        rows = [
            {
                "at": bar.timestamp.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "feed": bar.feed,
            }
            for bar in eligible
        ]
        reaction = await PriceReactionCalculator(_AsOfMarket(self.provider, packet.as_of)).compute(
            packet.company.symbol, packet.event_time, now=packet.as_of
        )
        return (
            ResearchDatum(
                provider="alpaca",
                kind="price_history_and_event_reaction",
                as_of=packet.as_of,
                text=json.dumps(
                    {
                        "symbol": packet.company.symbol,
                        "bars": rows,
                        "reaction": reaction.as_dict(),
                        "execution_pricing": False,
                    }
                ),
            ),
        )


class _AsOfMarket:
    """Give Phase 4's reaction calculator only completed bars at the cutoff."""

    def __init__(self, provider: MarketDataProvider, as_of: dt.datetime) -> None:
        self.provider = provider
        self.name = provider.name
        self.as_of = as_of

    async def latest_quote(self, symbol: str) -> Quote:
        raise ProviderResponseError("research uses a completed historical bar reference")

    async def latest_trade(self, symbol: str) -> Trade:
        raise ProviderResponseError("research does not request latest trades")

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> list[Bar]:
        try:
            bars = await self.provider.bars(
                symbol, timeframe, start, min(end, self.as_of), limit=limit
            )
        except ProviderError as exc:
            # Phase 4 includes exception text in reaction notes. Keep provider
            # bodies out of this packet while retaining the concrete error type.
            raise type(exc)("research bars unavailable") from None
        return sorted(
            (
                bar
                for bar in bars
                if start <= bar.timestamp
                and bar.timestamp + dt.timedelta(minutes=1) <= min(end, self.as_of)
            ),
            key=lambda bar: bar.timestamp,
        )

    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        raise ProviderResponseError("research snapshot is not an execution capability")

    async def aclose(self) -> None:
        pass
