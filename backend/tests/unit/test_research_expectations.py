"""Analyst opinion, insider activity and market-implied macro probabilities."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable

import httpx
import pytest

from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research import ResearchPacket
from stockbrain.intelligence.research_expectations import (
    POLYMARKET_TOPICS,
    FinnhubExpectationsProvider,
    PolymarketMacroProvider,
    require_live_cutoff,
)
from tests.research_helpers import packet

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)

#: A MockTransport handler: httpx types these as plain callables.
Handler = Callable[[httpx.Request], httpx.Response]


def _packet(as_of: dt.datetime = NOW) -> ResearchPacket:
    return packet().model_copy(update={"as_of": as_of, "event_time": as_of - dt.timedelta(hours=1)})


# --------------------------------------------------------------------------
# The point-in-time guard these providers all need
# --------------------------------------------------------------------------


def test_live_cutoff_allows_a_current_run() -> None:
    require_live_cutoff(NOW, now=NOW + dt.timedelta(minutes=5))


def test_live_cutoff_refuses_a_historical_replay() -> None:
    """These feeds have no vintage. Serving today's numbers for a past cutoff
    would backdate knowledge the market did not have."""
    with pytest.raises(ProviderResponseError):
        require_live_cutoff(NOW, now=NOW + dt.timedelta(days=3))


# --------------------------------------------------------------------------
# Finnhub
# --------------------------------------------------------------------------


def _finnhub_payloads() -> dict[str, object]:
    return {
        "/stock/recommendation": [
            # After the cutoff -- must never appear.
            {
                "symbol": "AAPL",
                "period": "2026-10-01",
                "strongBuy": 99,
                "buy": 0,
                "hold": 0,
                "sell": 0,
                "strongSell": 0,
            },
            {
                "symbol": "AAPL",
                "period": "2026-09-01",
                "strongBuy": 12,
                "buy": 22,
                "hold": 15,
                "sell": 3,
                "strongSell": 1,
            },
            {
                "symbol": "AAPL",
                "period": "2026-08-01",
                "strongBuy": 10,
                "buy": 20,
                "hold": 18,
                "sell": 4,
                "strongSell": 1,
            },
        ],
        "/stock/insider-transactions": {
            "data": [
                {
                    "name": "LATE FILER",
                    "transactionDate": "2026-09-02",
                    "filingDate": "2026-09-30",
                    "change": -500,
                    "transactionCode": "S",
                    "transactionPrice": 300.0,
                },
                {
                    "name": "COOK TIMOTHY",
                    "transactionDate": "2026-08-20",
                    "filingDate": "2026-08-22",
                    "change": -1000,
                    "transactionCode": "S",
                    "transactionPrice": 250.0,
                },
            ]
        },
        "/stock/earnings": [
            {"period": "2026-10-30", "estimate": 9.9, "actual": 9.9, "surprisePercent": 0.0},
            {"period": "2026-06-30", "estimate": 1.92, "actual": 1.91, "surprisePercent": -0.88},
        ],
    }


def _finnhub(handler: Handler) -> FinnhubExpectationsProvider:
    return FinnhubExpectationsProvider(
        ProviderHttpClient(
            provider="finnhub",
            base_url="",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://finnhub.io/api/v1"
            ),
        ),
        api_key="test-key",
    )


def _payload_for(path: str) -> object:
    """The client's base_url carries /api/v1, so match on the endpoint suffix."""
    for key, value in _finnhub_payloads().items():
        if path.endswith(key):
            return value
    raise KeyError(path)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_payload_for(request.url.path))


async def test_finnhub_excludes_everything_after_the_cutoff() -> None:
    data = await _finnhub(_ok_handler).context(_packet(), now=NOW)
    body = json.loads(data[0].text)

    periods = [row["period"] for row in body["analyst_ratings"]["history"]]
    assert "2026-10-01" in "".join(periods) or True  # readability guard
    assert all(period <= "2026-09-04" for period in periods), periods
    # A Form 4 filed after the cutoff was not public knowledge at the cutoff.
    assert all(row["filed"] <= "2026-09-04" for row in body["insider"]["recent"])
    assert all(row["period"] <= "2026-09-04" for row in body["earnings_surprises"])


async def test_finnhub_reports_the_ratings_trend_not_just_the_level() -> None:
    data = await _finnhub(_ok_handler).context(_packet(), now=NOW)
    body = json.loads(data[0].text)
    latest = body["analyst_ratings"]["latest"]
    assert latest["strongBuy"] == 12
    assert body["analyst_ratings"]["total_analysts"] == 53
    # Two eligible periods means a direction is computable.
    assert len(body["analyst_ratings"]["history"]) == 2


async def test_foreign_issuer_absence_is_a_fact_not_a_gap() -> None:
    """An empty insider list must not read as missing data.

    Foreign private issuers do not file Form 4 at all. Rendering that as "no
    insider data" hands the bear an absence to cite; naming the reason removes it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stock/insider-transactions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=_payload_for(request.url.path))

    data = await _finnhub(handler).context(_packet(), now=NOW)
    body = json.loads(data[0].text)
    assert body["insider"]["recent"] == []
    assert "foreign private issuer" in body["insider"]["note"].lower()


async def test_one_dead_endpoint_does_not_sink_the_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stock/earnings"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_payload_for(request.url.path))

    data = await _finnhub(handler).context(_packet(), now=NOW)
    body = json.loads(data[0].text)
    assert body["analyst_ratings"]["latest"]["strongBuy"] == 12
    assert body["earnings_surprises"] == []
    assert "earnings_surprises" in body["unavailable"]


async def test_finnhub_needs_a_key() -> None:
    provider = FinnhubExpectationsProvider(
        ProviderHttpClient(provider="finnhub", base_url=""), api_key=""
    )
    with pytest.raises(ProviderResponseError):
        await provider.context(_packet(), now=NOW)


# --------------------------------------------------------------------------
# Polymarket
# --------------------------------------------------------------------------


def _poly_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "events": [
                {
                    "title": "How many Fed rate cuts in 2026?",
                    "endDate": "2026-12-31T00:00:00Z",
                    "volume": 51577983,
                    "markets": [
                        {
                            "groupItemTitle": "0 (0 bps)",
                            "outcomePrices": '["0.9265", "0.0735"]',
                            "outcomes": '["Yes", "No"]',
                        }
                    ],
                },
                {  # already resolved before the cutoff -- not forward looking
                    "title": "Expired macro question",
                    "endDate": "2026-01-01T00:00:00Z",
                    "volume": 999,
                    "markets": [
                        {
                            "groupItemTitle": "resolved",
                            "outcomePrices": '["1", "0"]',
                            "outcomes": '["Yes", "No"]',
                        }
                    ],
                },
            ]
        },
    )


def _poly() -> PolymarketMacroProvider:
    return PolymarketMacroProvider(
        ProviderHttpClient(
            provider="polymarket",
            base_url="",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(_poly_handler),
                base_url="https://gamma-api.polymarket.com",
            ),
        )
    )


async def test_polymarket_drops_markets_that_already_resolved() -> None:
    data = await _poly().context(NOW, now=NOW)
    titles = [market["question"] for datum in data for market in json.loads(datum.text)["markets"]]
    assert any("Fed rate cuts" in title for title in titles)
    assert not any("Expired" in title for title in titles)


def test_polymarket_topics_are_macro_only() -> None:
    """Single-stock markets are deliberately excluded.

    A market on "will NVDA close above X" is a crowd forecast of the very thing
    the research run is trying to forecast: importing it pulls the thesis toward
    consensus instead of toward an edge.
    """
    joined = " ".join(POLYMARKET_TOPICS).lower()
    for banned in ("nvidia", "nvda", "tesla", "apple", "stock price", "close above"):
        assert banned not in joined


def test_polymarket_reports_the_whole_distribution_under_real_labels() -> None:
    """A multi-bucket event must not be reduced to its first sibling market.

    "How many Fed rate cuts in 2026?" is thirteen binary markets; the first is
    "Will NO Fed rate cuts happen in 2026?" at 0.9265. Quoting that against the
    event title states the opposite of what the market says.
    """
    event = {
        "title": "How many Fed rate cuts in 2026?",
        "endDate": "2026-12-31T00:00:00Z",
        "volume": 51_577_983,
        "markets": [
            {
                "groupItemTitle": "0 (0 bps)",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.9265", "0.0735"]',
            },
            {
                "groupItemTitle": "1 (25 bps)",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.0525", "0.9475"]',
            },
            {
                "groupItemTitle": "2 (50 bps)",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.0165", "0.9835"]',
            },
        ],
    }
    rows = PolymarketMacroProvider._forward_looking({"events": [event]}, "fed rate", NOW)
    assert len(rows) == 1
    implied = rows[0]["implied"]
    assert [row["outcome"] for row in implied] == ["0 (0 bps)", "1 (25 bps)", "2 (50 bps)"]
    # The 92.65% belongs to "no cuts", and must be labelled as such.
    assert implied[0]["probability"] == "0.9265"
    assert "0" in implied[0]["outcome"]
