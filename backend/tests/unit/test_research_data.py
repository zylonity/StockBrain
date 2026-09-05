"""FRED requests and supplemental research failure contracts."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from stockbrain.enums import BarTimeframe
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research_data import (
    AlpacaResearchProvider,
    FredMacroProvider,
    fred_error,
)
from stockbrain.market_data.base import Bar, MarketDataProvider
from tests.research_helpers import packet


@pytest.mark.parametrize("value", ["4.25", "."])
async def test_fred_request_dates_vintage_and_missing_values(value: str) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "date": "2026-09-02",
                        "value": value,
                        "realtime_start": "2026-09-03",
                        "realtime_end": "2026-09-03",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.stlouisfed.org/fred/"
    ) as http:
        client = FredMacroProvider(
            SecretStr("not-a-real-key"),
            series=("DFF",),
            http=ProviderHttpClient(
                provider="fred", base_url="", client=http, refine_error=fred_error
            ),
        )
        result = await client.context(packet().as_of)
    query = observed[0].url.params
    assert query["series_id"] == "DFF" and query["file_type"] == "json"
    assert query["realtime_start"] == query["realtime_end"] == "2026-09-03"
    assert query["observation_end"] == "2026-09-03"
    assert query["limit"] == "100" and query["units"] == "lin"
    assert "not-a-real-key" not in result[0].model_dump_json()
    assert json.loads(result[0].text)["observations"][0]["value"] == (
        None if value == "." else value
    )


async def test_fred_missing_key_never_requests() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("HTTP with no key"))
    ) as http:
        client = FredMacroProvider(
            SecretStr(""), http=ProviderHttpClient(provider="fred", base_url="", client=http)
        )
        with pytest.raises(ProviderAuthError, match="missing"):
            await client.context(packet().as_of)


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, ProviderAuthError),
        (401, ProviderAuthError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (423, ProviderUnavailable),
    ],
)
async def test_fred_provider_errors(status: int, expected: type[Exception]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, json={"error_message": "api_key secret-canary is not registered"}
            )
        ),
        base_url="https://api.stlouisfed.org/fred/",
    ) as http:
        client = FredMacroProvider(
            SecretStr("secret-canary"),
            series=("DFF",),
            http=ProviderHttpClient(
                provider="fred", base_url="", client=http, max_attempts=1, refine_error=fred_error
            ),
        )
        with pytest.raises(expected) as error:
            await client.context(packet().as_of)
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"observations": "invalid"},
        {"observations": [{"date": "bad"}]},
        {
            "observations": [
                {
                    "date": "2026-09-10",
                    "value": "1",
                    "realtime_start": "2026-09-03",
                    "realtime_end": "2026-09-03",
                }
            ]
        },
        {
            "observations": [
                {
                    "date": "2026-09-02",
                    "value": "NaN",
                    "realtime_start": "2026-09-03",
                    "realtime_end": "2026-09-03",
                }
            ]
        },
    ],
)
async def test_fred_malformed_response(payload: object) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        base_url="https://api.stlouisfed.org/fred/",
    ) as http:
        client = FredMacroProvider(
            SecretStr("key"), http=ProviderHttpClient(provider="fred", base_url="", client=http)
        )
        with pytest.raises(ProviderResponseError):
            await client.context(packet().as_of)


def test_fred_catalogue_is_bounded() -> None:
    with pytest.raises(ValueError):
        FredMacroProvider(SecretStr("key"), series=("EVERY_SERIES",))


async def test_alpaca_reaction_uses_only_completed_historical_bars() -> None:
    value = packet()
    provider = AsyncMock(spec=MarketDataProvider)
    provider.name = "alpaca"

    def bar(at: dt.datetime, price: int) -> Bar:
        return Bar(
            symbol="AAPL",
            timestamp=at,
            open=Decimal(price),
            high=Decimal(price),
            low=Decimal(price),
            close=Decimal(price),
            volume=100,
            feed="iex",
        )

    async def bars(
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> list[Bar]:
        assert symbol == "AAPL" and end <= value.as_of
        if timeframe == BarTimeframe.DAY_1:
            # Deliberately unsorted, with a bar which has not yet completed.
            return [bar(value.as_of, 999), bar(value.as_of - dt.timedelta(days=2), 90)]
        return [
            bar(value.as_of, 999),
            bar(value.as_of - dt.timedelta(minutes=1), 110),
            bar(value.event_time, 100),
        ]

    provider.bars.side_effect = bars
    data = await AlpacaResearchProvider(provider).context(value)
    result = json.loads(data[0].text)
    assert len(result["bars"]) == 1
    assert "999" not in data[0].text
    assert result["reaction"]["percent_move"] == "10.0000"
    assert result["reaction"]["reference_price_basis"] == "last_bar_close"
    assert result["reaction"]["feed"] == "iex"
    assert result["execution_pricing"] is False
    provider.latest_quote.assert_not_called()
    provider.latest_trade.assert_not_called()
    provider.capability.assert_not_called()


async def test_alpaca_missing_history_is_explicit() -> None:
    provider = AsyncMock(spec=MarketDataProvider)
    provider.bars.return_value = []
    with pytest.raises(ProviderResponseError, match="no completed"):
        await AlpacaResearchProvider(provider).context(packet())
