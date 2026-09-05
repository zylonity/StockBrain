"""Alpaca market data, entitlement detection and the sizing guard.

Payloads follow Alpaca's current OpenAPI definition (2026-09-04).  The tests
that matter most are the ones about *provenance*: a price that cannot say where
it came from and how old it is must never be usable, and a 403 that means
"you have not paid for SIP" must never be mistaken for "your key is wrong".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.enums import (
    EXECUTION_GRADE_PRICE_SOURCES,
    BarTimeframe,
    CapabilityState,
    MarketSession,
    PriceSource,
)
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.market_data.alpaca import (
    ALPACA_HISTORICAL_FEEDS,
    ALPACA_LATEST_FEEDS,
    AlpacaMarketDataClient,
    refine_alpaca_error,
)
from stockbrain.market_data.base import ProviderCapability, Quote, quote_blockers, to_decimal
from stockbrain.market_data.sessions import session_from_schedule, session_from_us_clock

QUOTE_BODY = {
    "symbol": "AAPL",
    "quote": {
        "t": "2026-09-04T15:35:08.946977536Z",
        "bx": "V",
        "bp": 172.6,
        "bs": 2,
        "ax": "V",
        "ap": 172.7,
        "as": 1,
        "c": ["R"],
        "z": "C",
    },
}

TRADE_BODY = {
    "symbol": "AAPL",
    "trade": {
        "t": "2026-09-04T15:35:09.100000000Z",
        "x": "V",
        "p": 172.65,
        "s": 100,
        "c": ["@"],
        "i": 12727,
        "z": "C",
    },
}

BARS_BODY = {
    "symbol": "AAPL",
    "bars": [
        {
            "t": "2026-09-04T14:00:00Z",
            "o": 170.1,
            "h": 170.9,
            "l": 169.8,
            "c": 170.5,
            "v": 60937,
            "n": 1727,
            "vw": 170.42,
        }
    ],
    "next_page_token": None,
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "alpaca_api_key": "PK-test",
        "alpaca_api_secret": "secret-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(handler: httpx.MockTransport, **overrides: object) -> AlpacaMarketDataClient:
    settings = _settings(**overrides)
    http = ProviderHttpClient(
        provider="alpaca_market_data",
        base_url=settings.alpaca_data_base_url,
        client=httpx.AsyncClient(transport=handler, base_url=settings.alpaca_data_base_url),
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
        refine_error=refine_alpaca_error,
    )
    return AlpacaMarketDataClient(settings, http=http)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------
def test_the_feed_is_always_sent_explicitly() -> None:
    """Alpaca's default feed depends on the account's subscription.

    "``sip`` if the user has the unlimited subscription, otherwise ``iex``" means
    an omitted parameter has a different meaning per account -- the same trap as
    DeepSeek's ``thinking`` default.
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("feed"))
        return httpx.Response(200, json=QUOTE_BODY)

    async def run() -> None:
        client = _client(httpx.MockTransport(handler))
        await client.latest_quote("aapl")
        await client.aclose()

    import asyncio

    asyncio.run(run())
    assert seen == ["iex"]


def test_delayed_sip_is_not_a_legal_bars_feed() -> None:
    """The historical enum omits ``delayed_sip`` and ``overnight``.

    Forwarding the configured feed blindly would produce a 400 on every bars
    request for a deployment that legitimately uses delayed SIP for quotes.
    """
    assert "delayed_sip" in ALPACA_LATEST_FEEDS
    assert "delayed_sip" not in ALPACA_HISTORICAL_FEEDS
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(200, json=BARS_BODY)),
        alpaca_stock_feed="delayed_sip",
    )
    assert client.feed == "delayed_sip"
    assert client.historical_feed() == "iex"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
async def test_a_valid_iex_quote_maps_every_documented_field() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=QUOTE_BODY)))
    quote = await client.latest_quote("AAPL")
    await client.aclose()

    assert quote.symbol == "AAPL"
    assert quote.feed == "iex"
    assert quote.price_source is PriceSource.ALPACA_IEX
    assert quote.bid == Decimal("172.6")
    assert quote.ask == Decimal("172.7")
    assert quote.bid_size == 2
    # The wire field is `as`, a Python keyword; the alias must still land.
    assert quote.ask_size == 1
    assert quote.tape == "C"
    assert quote.conditions == ("R",)
    assert quote.is_two_sided
    assert quote.provider_timestamp == dt.datetime(2026, 9, 4, 15, 35, 8, 946977, tzinfo=dt.UTC)


async def test_prices_are_decimal_with_no_binary_float_artefacts() -> None:
    """A price is money. ``Decimal(0.1)`` would persist 0.10000000000000000555."""
    body = {"symbol": "X", "quote": {**QUOTE_BODY["quote"], "bp": 0.1, "ap": 0.3}}  # type: ignore[dict-item]
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    quote = await client.latest_quote("X")
    await client.aclose()

    assert quote.bid == Decimal("0.1")
    assert str(quote.bid) == "0.1"
    assert str(quote.ask) == "0.3"
    assert quote.price == Decimal("0.2")
    assert isinstance(quote.price, Decimal)


def test_to_decimal_round_trips_the_json_literal() -> None:
    assert to_decimal(172.7) == Decimal("172.7")
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(None) is None
    assert to_decimal(Decimal("1.5")) == Decimal("1.5")


async def test_latest_trade_parses_the_documented_shape() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=TRADE_BODY)))
    trade = await client.latest_trade("AAPL")
    await client.aclose()
    assert trade.price == Decimal("172.65")
    assert trade.size == 100
    assert trade.exchange == "V"
    assert trade.price_source is PriceSource.ALPACA_IEX


async def test_bars_parse_and_record_the_feed_used() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=BARS_BODY)))
    bars = await client.bars(
        "AAPL",
        BarTimeframe.MIN_1,
        dt.datetime(2026, 9, 4, 13, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 4, 15, tzinfo=dt.UTC),
    )
    await client.aclose()
    assert len(bars) == 1
    assert bars[0].open == Decimal("170.1")
    assert bars[0].close == Decimal("170.5")
    assert bars[0].volume == 60937
    assert bars[0].vwap == Decimal("170.42")
    assert bars[0].feed == "iex"


async def test_a_malformed_quote_is_rejected_not_coerced() -> None:
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(200, json={"symbol": "AAPL", "quote": None}))
    )
    with pytest.raises(ProviderResponseError):
        await client.latest_quote("AAPL")
    await client.aclose()


async def test_a_quote_missing_its_timestamp_is_rejected() -> None:
    """A price without a timestamp cannot have an age, so it cannot be judged."""
    body = {"symbol": "AAPL", "quote": {"bp": 1.0, "ap": 1.1}}
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(ProviderResponseError):
        await client.latest_quote("AAPL")
    await client.aclose()


# ---------------------------------------------------------------------------
# Entitlement vs authentication
# ---------------------------------------------------------------------------
def test_an_entitlement_403_is_not_an_auth_failure() -> None:
    """Alpaca answers 403 for both. Only the body tells them apart.

    Getting this wrong would mark a perfectly good credential as broken and stop
    the news stream over a market-data subscription the account never had.
    """
    response = httpx.Response(
        403,
        json={"code": 42210000, "message": "subscription does not permit querying recent SIP data"},
        request=httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest"),
    )
    refined = refine_alpaca_error(response, ProviderAuthError("alpaca: rejected"))
    assert isinstance(refined, ProviderEntitlementError)


def test_a_genuine_credential_403_stays_an_auth_failure() -> None:
    response = httpx.Response(
        403,
        json={"message": "forbidden"},
        request=httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest"),
    )
    refined = refine_alpaca_error(response, ProviderAuthError("alpaca: rejected"))
    assert isinstance(refined, ProviderAuthError)
    assert not isinstance(refined, ProviderEntitlementError)


def test_refinement_ignores_a_non_json_body() -> None:
    response = httpx.Response(
        403,
        content=b"<html>forbidden</html>",
        request=httpx.Request("GET", "https://data.alpaca.markets/"),
    )
    original = ProviderAuthError("alpaca: rejected")
    assert refine_alpaca_error(response, original) is original


async def test_capability_reports_entitlement_missing_without_crashing() -> None:
    client = _client(
        httpx.MockTransport(
            lambda _: httpx.Response(
                403,
                json={
                    "code": 42210000,
                    "message": "subscription does not permit querying recent SIP data",
                },
            )
        ),
        alpaca_stock_feed="sip",
    )
    capability = await client.capability()
    await client.aclose()

    assert capability.state is CapabilityState.ENTITLEMENT_MISSING
    assert capability.realtime_pricing_usable is False
    assert capability.feed == "sip"
    # Degraded, not down: SIP is unavailable but nothing else in the system is.
    assert capability.state.to_provider_status().value == "DEGRADED"


async def test_capability_reports_auth_failed_separately() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(401, json={"message": "nope"})))
    capability = await client.capability()
    await client.aclose()
    assert capability.state is CapabilityState.AUTH_FAILED
    assert capability.state.to_provider_status().value == "DOWN"
    assert capability.realtime_pricing_usable is False


async def test_capability_reports_down_for_a_transient_outage() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(503, json={})))
    capability = await client.capability()
    await client.aclose()
    assert capability.state is CapabilityState.DOWN
    assert capability.realtime_pricing_usable is False


async def test_capability_is_healthy_on_a_two_sided_quote() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=QUOTE_BODY)))
    capability = await client.capability()
    await client.aclose()
    assert capability.state is CapabilityState.HEALTHY
    assert capability.realtime_pricing_usable is True
    assert capability.probe_symbol == "AAPL"


async def test_a_one_sided_probe_quote_degrades_rather_than_passes() -> None:
    """Alpaca documents 0 as "no active bid/ask", not a price of zero."""
    body = {"symbol": "AAPL", "quote": {**QUOTE_BODY["quote"], "bp": 0, "bs": 0}}  # type: ignore[dict-item]
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    capability = await client.capability()
    await client.aclose()
    assert capability.state is CapabilityState.DEGRADED
    assert capability.realtime_pricing_usable is False


async def test_a_stale_probe_quote_stays_healthy_but_is_flagged() -> None:
    """Observed live on 2026-09-04 at 03:40 UTC: IEX returned the 20:00 close
    print, 7.7 hours old, and the capability reported itself usable.

    That was accurate -- the feed *is* entitled -- and unreadable next to a 15s
    sizing limit. Entitlement and freshness are now reported separately: the
    state stays HEALTHY, because folding closing prints into DEGRADED would make
    the subsystem look broken every night, and the staleness is stated outright.
    """
    stale_body = {
        "symbol": "AAPL",
        "quote": {**QUOTE_BODY["quote"], "t": "2026-09-04T20:00:00.006211Z"},  # type: ignore[dict-item]
    }
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=stale_body)))
    capability = await client.capability()
    await client.aclose()

    assert capability.state is CapabilityState.HEALTHY
    assert capability.realtime_pricing_usable is True
    assert capability.probe_quote_stale is True
    assert capability.probe_quote_age_ms is not None
    assert capability.probe_quote_age_ms > 15_000
    assert any("older than the 15s sizing limit" in reason for reason in capability.blockers)
    assert any("outside market hours" in reason for reason in capability.blockers)


async def test_a_fresh_probe_quote_is_not_flagged_stale() -> None:
    """The flag must not fire during market hours, or it is worthless."""
    import datetime as real_dt

    fresh = {
        "symbol": "AAPL",
        "quote": {
            **QUOTE_BODY["quote"],  # type: ignore[dict-item]
            "t": real_dt.datetime.now(real_dt.UTC).isoformat().replace("+00:00", "Z"),
        },
    }
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=fresh)))
    capability = await client.capability()
    await client.aclose()

    assert capability.state is CapabilityState.HEALTHY
    assert capability.probe_quote_stale is False
    assert capability.blockers == ()


async def test_a_stale_quote_is_still_refused_for_sizing() -> None:
    """The staleness flag changes reporting, never the sizing decision.

    The live probe's own quote carried a 33-dollar AAPL spread and a 7.7-hour
    age; `quote_blockers` refused it on age, and must keep doing so.
    """
    stale_body = {
        "symbol": "AAPL",
        "quote": {**QUOTE_BODY["quote"], "t": "2026-09-04T20:00:00.006211Z"},  # type: ignore[dict-item]
    }
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=stale_body)))
    capability = await client.capability()
    quote = await client.latest_quote("AAPL")
    await client.aclose()

    blockers = quote_blockers(quote, capability, max_age_seconds=15)
    assert blockers, "a closing print must never be usable for sizing"
    assert any("older than" in reason for reason in blockers)


async def test_capability_is_cached_until_refresh_is_requested() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=QUOTE_BODY)

    client = _client(httpx.MockTransport(handler))
    await client.capability()
    await client.capability()
    assert calls == 1, "the probe must not fire on every consultation"
    await client.capability(refresh=True)
    await client.aclose()
    assert calls == 2


async def test_a_transport_failure_is_transient_not_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        await client.latest_quote("AAPL")
    await client.aclose()


# ---------------------------------------------------------------------------
# The sizing guard
# ---------------------------------------------------------------------------
def _quote(**overrides: object) -> Quote:
    now = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
    base: dict[str, object] = {
        "symbol": "AAPL",
        "provider": "alpaca_market_data",
        "feed": "iex",
        "price_source": PriceSource.ALPACA_IEX,
        "provider_timestamp": now,
        "received_at": now + dt.timedelta(milliseconds=200),
        "bid": Decimal("172.6"),
        "ask": Decimal("172.7"),
    }
    base.update(overrides)
    return Quote(**base)  # type: ignore[arg-type]


def _healthy() -> ProviderCapability:
    return ProviderCapability(
        provider="alpaca_market_data",
        state=CapabilityState.HEALTHY,
        feed="iex",
        realtime_pricing_usable=True,
    )


def test_a_fresh_two_sided_iex_quote_has_no_sizing_blockers() -> None:
    assert quote_blockers(_quote(), _healthy(), max_age_seconds=15) == []


def test_a_stale_quote_blocks_sizing() -> None:
    now = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
    stale = _quote(received_at=now + dt.timedelta(seconds=60))
    blockers = quote_blockers(stale, _healthy(), max_age_seconds=15)
    assert any("older than" in reason for reason in blockers)


def test_a_one_sided_quote_blocks_sizing() -> None:
    blockers = quote_blockers(_quote(bid=Decimal(0)), _healthy(), max_age_seconds=15)
    assert any("two-sided" in reason for reason in blockers)


def test_trading212_prices_can_never_size_an_order() -> None:
    """T212 API market data is not guaranteed real-time; it is display only.

    This is the rule from spec section 12, expressed where it cannot be
    forgotten: the price source is simply not in the execution-grade set.
    """
    assert PriceSource.BROKER_T212 not in EXECUTION_GRADE_PRICE_SOURCES
    assert PriceSource.YFINANCE not in EXECUTION_GRADE_PRICE_SOURCES
    blockers = quote_blockers(
        _quote(price_source=PriceSource.BROKER_T212), _healthy(), max_age_seconds=15
    )
    assert any("display/reconciliation only" in reason for reason in blockers)


def test_missing_capability_blocks_sizing() -> None:
    assert quote_blockers(_quote(), None, max_age_seconds=15)


def test_a_missing_quote_blocks_sizing() -> None:
    blockers = quote_blockers(None, _healthy(), max_age_seconds=15)
    assert any("no quote" in reason for reason in blockers)


def test_an_entitlement_gap_blocks_sizing_even_with_a_quote() -> None:
    degraded = ProviderCapability(
        provider="alpaca_market_data",
        state=CapabilityState.ENTITLEMENT_MISSING,
        realtime_pricing_usable=False,
    )
    assert quote_blockers(_quote(), degraded, max_age_seconds=15)


def test_quote_age_is_clamped_at_zero() -> None:
    """A provider clock slightly ahead of ours is not a quote from the future."""
    now = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
    quote = _quote(provider_timestamp=now, received_at=now - dt.timedelta(seconds=1))
    assert quote.age_ms == 0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_the_broker_schedule_decides_the_session_when_it_covers_the_instant() -> None:
    events: list[dict[str, object]] = [
        {"date": "2026-09-04T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-09-04T13:30:00Z", "type": "OPEN"},
        {"date": "2026-09-04T20:00:00Z", "type": "CLOSE"},
    ]
    verdict = session_from_schedule(events, dt.datetime(2026, 9, 4, 15, tzinfo=dt.UTC))
    assert verdict.session is MarketSession.REGULAR
    assert verdict.source == "exchange_schedule"
    assert verdict.holiday_aware is True

    pre = session_from_schedule(events, dt.datetime(2026, 9, 4, 9, tzinfo=dt.UTC))
    assert pre.session is MarketSession.PRE_MARKET
    after = session_from_schedule(events, dt.datetime(2026, 9, 4, 21, tzinfo=dt.UTC))
    assert after.session is MarketSession.CLOSED


def test_a_schedule_that_does_not_cover_the_instant_says_unknown() -> None:
    """Extrapolating past the schedule's edge would be a guess wearing its authority."""
    events: list[dict[str, object]] = [{"date": "2026-09-04T13:30:00Z", "type": "OPEN"}]
    verdict = session_from_schedule(events, dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
    assert verdict.session is MarketSession.UNKNOWN
    assert session_from_schedule([], dt.datetime(2026, 9, 4, tzinfo=dt.UTC)).session is (
        MarketSession.UNKNOWN
    )


def test_the_us_clock_fallback_splits_pre_regular_and_after_hours() -> None:
    # 2026-09-04 is a Friday. Times below are US/Eastern.
    assert (
        session_from_us_clock(
            dt.datetime(2026, 9, 4, 11, 0, tzinfo=dt.UTC)  # 07:00 ET
        ).session
        is MarketSession.PRE_MARKET
    )
    assert (
        session_from_us_clock(
            dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)  # 11:00 ET
        ).session
        is MarketSession.REGULAR
    )
    assert (
        session_from_us_clock(
            dt.datetime(2026, 9, 4, 21, 0, tzinfo=dt.UTC)  # 17:00 ET
        ).session
        is MarketSession.AFTER_HOURS
    )
    assert (
        session_from_us_clock(
            dt.datetime(2026, 9, 5, 3, 0, tzinfo=dt.UTC)  # 23:00 ET Friday
        ).session
        is MarketSession.CLOSED
    )
    # Saturday
    assert (
        session_from_us_clock(dt.datetime(2026, 9, 5, 15, 0, tzinfo=dt.UTC)).session
        is MarketSession.CLOSED
    )


def test_the_clock_fallback_admits_it_is_not_holiday_aware() -> None:
    verdict = session_from_us_clock(dt.datetime(2026, 9, 4, 15, tzinfo=dt.UTC))
    assert verdict.source == "us_clock"
    assert verdict.holiday_aware is False
