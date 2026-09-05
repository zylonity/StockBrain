"""Live, read-only provider checks for Phase 4.

Run explicitly::

    pytest -m live -s tests/integration/test_phase4_live.py

These are the only tests that touch a real provider, and they are strictly
read-only: two metadata GETs against the Trading 212 **demo** environment and
two market-data GETs against Alpaca.  No order endpoint is called, no mutation
endpoint of any kind is reachable from the clients under test, and the suite is
deliberately one request per fact so a re-run does not burn an API budget that
allows one instruments call per fifty seconds.

Without credentials each test skips with a reason.  Skipping is the correct
outcome: a faked pass here would be a claim that a provider contract was
verified when it was not.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from stockbrain.broker.trading212_metadata import Trading212MetadataClient
from stockbrain.config import Settings
from stockbrain.enums import CapabilityState
from stockbrain.market_data.alpaca import AlpacaMarketDataClient

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _t212_settings() -> Settings:
    key = os.environ.get("T212_API_KEY", "")
    secret = os.environ.get("T212_API_SECRET", "")
    if not key or not secret:
        pytest.skip("T212_API_KEY / T212_API_SECRET are not set")
    return Settings(
        app_env="test",
        t212_api_key=key,
        t212_api_secret=secret,
        # Demo only. A live-environment metadata call is still read-only, but
        # this phase has no business authenticating against the live account.
        t212_env="demo",
    )


def _alpaca_settings() -> Settings:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        pytest.skip("ALPACA_API_KEY / ALPACA_API_SECRET are not set")
    return Settings(
        app_env="test",
        alpaca_api_key=key,
        alpaca_api_secret=secret,
        alpaca_stock_feed=os.environ.get("ALPACA_STOCK_FEED", "iex"),
    )


async def test_live_trading212_demo_exchanges() -> None:
    """Authenticate against the demo API and read exchange metadata.

    Prints the observed contract, because recording what the provider actually
    returns is the entire point of a live smoke test.
    """
    settings = _t212_settings()
    client = Trading212MetadataClient(settings)
    try:
        exchanges = await client.fetch_exchanges()
    finally:
        snapshot = client.rate_limit_snapshot()
        await client.aclose()

    assert exchanges, "the demo account should see at least one exchange"
    first = exchanges[0]
    print(f"\nbase url:            {settings.t212_base_url}")
    print(f"exchanges returned:  {len(exchanges)}")
    print(f"first exchange:      id={first.id} name={first.name!r}")
    print(f"working schedules:   {len(first.working_schedules)}")
    if first.working_schedules:
        events = first.working_schedules[0].time_events
        print(f"time event types:    {sorted({e.type for e in events if e.type})}")
    print(f"rate limit headers:  {snapshot}")


async def test_live_trading212_demo_instruments() -> None:
    """Read instrument metadata and report which fields are actually supplied."""
    settings = _t212_settings()
    client = Trading212MetadataClient(settings)
    try:
        instruments = await client.fetch_instruments()
    finally:
        snapshot = client.rate_limit_snapshot()
        await client.aclose()

    assert instruments
    supplied = {
        name
        for instrument in instruments[:500]
        for name, value in instrument.model_dump().items()
        if value is not None
    }
    types = sorted({instrument.type for instrument in instruments if instrument.type})
    with_isin = sum(1 for instrument in instruments if instrument.isin)
    with_schedule = sum(1 for instrument in instruments if instrument.working_schedule_id)

    print(f"\ninstruments:         {len(instruments)}")
    print(f"fields supplied:     {sorted(supplied)}")
    print(f"instrument types:    {types}")
    print(f"with isin:           {with_isin}")
    print(f"with schedule id:    {with_schedule}")
    print(f"sample ticker:       {instruments[0].ticker}")
    print(f"rate limit headers:  {snapshot}")

    # The documented response has no exchange field at all; the sync derives it.
    assert "exchange" not in supplied


async def test_live_alpaca_capability_and_quote() -> None:
    """Entitlement probe plus one quote for a liquid US equity."""
    settings = _alpaca_settings()
    client = AlpacaMarketDataClient(settings)
    try:
        capability = await client.capability(refresh=True)
        print(f"\nfeed configured:     {client.feed}")
        print(f"capability state:    {capability.state.value}")
        print(f"pricing usable:      {capability.realtime_pricing_usable}")
        print(f"detail:              {capability.detail}")
        print(f"probe quote age ms:  {capability.probe_quote_age_ms}")

        if capability.state is not CapabilityState.HEALTHY:
            pytest.skip(f"alpaca capability is {capability.state.value}: {capability.detail}")

        quote = await client.latest_quote(settings.market_data_probe_symbol)
    finally:
        await client.aclose()

    print(f"symbol:              {quote.symbol}")
    print(f"price source:        {quote.price_source.value}")
    print(f"bid / ask:           {quote.bid} / {quote.ask}")
    print(f"mid:                 {quote.price}")
    print(f"provider timestamp:  {quote.provider_timestamp.isoformat()}")
    print(f"quote age ms:        {quote.age_ms}")
    print(f"tape / conditions:   {quote.tape} / {list(quote.conditions)}")

    assert isinstance(quote.price, Decimal)
    assert quote.age_ms >= 0
