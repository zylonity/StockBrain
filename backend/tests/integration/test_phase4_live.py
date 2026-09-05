"""Live, read-only provider verification for Phase 4.

Run explicitly::

    pytest -m live -s tests/integration/test_phase4_live.py

Strictly read-only, and strictly minimal: **four GETs in total** -- two Trading
212 metadata calls against the **demo** environment and two Alpaca market-data
calls.  No order endpoint is called; the Trading 212 client reachable from here
has no order method to call.  Nothing is written to the database.

Credentials come from the git-ignored ``.env`` at the repository root (the
process environment wins if both are set).  Nothing from either is printed:
only *derived facts* about the responses reach stdout.

Without credentials each test skips with a reason.  Skipping is the correct
outcome -- a faked pass would be a claim that a provider contract was verified
when it was not.

What this file is for is the questions the unit tests cannot answer, because
they only know what the documentation says:

* which instrument fields Trading 212 *actually* populates;
* how often ISIN is present on the STOCK/ETF rows resolution depends on;
* whether ``shortName`` is consistently usable as a market-data symbol;
* whether ``workingScheduleId`` reliably resolves to an exchange;
* which rate-limit headers really come back;
* whether the configured Alpaca feed is genuinely entitled, and how fresh its
  quotes are.
"""

from __future__ import annotations

import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.broker.trading212_metadata import Trading212MetadataClient
from stockbrain.config import Settings
from stockbrain.enums import CapabilityState
from stockbrain.instruments.normalize import normalize_ticker, split_broker_ticker
from stockbrain.market_data.alpaca import AlpacaMarketDataClient
from stockbrain.market_data.base import quote_blockers

pytestmark = [pytest.mark.live, pytest.mark.integration]

#: The git-ignored .env lives at the repository root, one level above `backend/`.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

#: Instrument types instrument resolution will actually price and size.  ISIN
#: coverage only matters for these; a FOREX or CORPACT row without one is not a
#: gap.
_TRADABLE_TYPES = frozenset({"STOCK", "ETF"})


def _env(name: str) -> str:
    """Read one credential from the process environment or the root ``.env``.

    The value is returned, never logged. Only the *presence* of a credential is
    ever reported, and only as a skip reason.
    """
    from_process = os.environ.get(name)
    if from_process:
        return from_process
    if not _ENV_FILE.is_file():
        return ""
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != name:
            continue
        # Strip an inline `# comment` and surrounding quotes, as pydantic-settings
        # does, so a commented .env line does not become part of a credential.
        value = value.split(" #", 1)[0].strip().strip("'\"")
        return value
    return ""


def _t212_settings() -> Settings:
    key = _env("T212_API_KEY")
    secret = _env("T212_API_SECRET")
    if not key or not secret:
        pytest.skip(
            "T212_API_KEY / T212_API_SECRET are not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )
    return Settings(
        app_env="test",
        t212_api_key=key,
        t212_api_secret=secret,
        # Demo only. A live-environment metadata call would still be read-only,
        # but this phase has no business authenticating against the live account.
        t212_env="demo",
    )


def _alpaca_settings() -> Settings:
    key = _env("ALPACA_API_KEY")
    secret = _env("ALPACA_API_SECRET")
    if not key or not secret:
        pytest.skip(
            "ALPACA_API_KEY / ALPACA_API_SECRET are not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )
    return Settings(
        app_env="test",
        alpaca_api_key=key,
        alpaca_api_secret=secret,
        alpaca_stock_feed=_env("ALPACA_STOCK_FEED") or "iex",
    )


def _pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{part / whole * 100:.2f}%"


async def test_live_trading212_demo_metadata() -> None:
    """Two GETs against the demo API: exchanges, then instruments.

    Both endpoints in one test because the interesting question needs both:
    whether an instrument's ``workingScheduleId`` actually resolves to an
    exchange. Splitting them would either duplicate a call or leave that
    unanswerable.
    """
    settings = _t212_settings()
    client = Trading212MetadataClient(settings)
    try:
        exchanges = await client.fetch_exchanges()
        exchange_rate_limit = client.rate_limit_snapshot()
        instruments = await client.fetch_instruments()
        instrument_rate_limit = client.rate_limit_snapshot()
    finally:
        await client.aclose()

    # --- 1/3: authentication and both endpoints answered -----------------
    assert exchanges, "the demo account should see at least one exchange"
    assert instruments, "the demo account should see at least one instrument"

    schedule_to_exchange = {
        schedule.id: (exchange.id, exchange.name)
        for exchange in exchanges
        for schedule in exchange.working_schedules
    }

    print(f"\nbase url:                {settings.t212_base_url}")
    print(f"exchanges returned:      {len(exchanges)}")
    print(f"working schedules:       {len(schedule_to_exchange)}")
    print(f"instruments returned:    {len(instruments)}")

    # --- 4: which fields are actually populated --------------------------
    populated: Counter[str] = Counter()
    for instrument in instruments:
        for name, value in instrument.model_dump().items():
            if value is not None:
                populated[name] += 1
    total = len(instruments)
    print("\nfield population (non-null / total):")
    for name in sorted(instrument.model_dump()):
        print(f"  {name:22} {populated[name]:>7} / {total}  {_pct(populated[name], total)}")

    # The documented response has no exchange field at all; the sync derives it.
    assert "exchange" not in populated

    types = Counter(instrument.type or "<null>" for instrument in instruments)
    print(f"\ninstrument types:        {dict(types.most_common())}")

    # --- 5: ISIN coverage on the rows resolution depends on --------------
    tradable = [i for i in instruments if (i.type or "").upper() in _TRADABLE_TYPES]
    tradable_with_isin = sum(1 for i in tradable if i.isin)
    print(f"\nSTOCK/ETF rows:          {len(tradable)}")
    print(
        f"  with ISIN:             {tradable_with_isin} ({_pct(tradable_with_isin, len(tradable))})"
    )
    by_type_isin = {
        kind: (
            sum(1 for i in instruments if (i.type or "").upper() == kind and i.isin),
            sum(1 for i in instruments if (i.type or "").upper() == kind),
        )
        for kind in sorted(_TRADABLE_TYPES)
    }
    for kind, (with_isin, count) in by_type_isin.items():
        print(f"  {kind:22} {with_isin} / {count}  {_pct(with_isin, count)}")

    # --- 6: is shortName usable as a market symbol? ----------------------
    with_short = [i for i in instruments if i.short_name]
    dirty = [
        i
        for i in with_short
        if i.short_name and normalize_ticker(i.short_name) != i.short_name.strip().upper()
    ]
    disagreeing = [
        i
        for i in instruments
        if i.short_name
        and normalize_ticker(i.short_name) != normalize_ticker(split_broker_ticker(i.ticker)[0])
    ]
    symbol_counts = Counter(normalize_ticker(i.short_name) for i in with_short if i.short_name)
    reused = [symbol for symbol, count in symbol_counts.items() if count > 1]
    print(f"\nshortName present:       {len(with_short)} ({_pct(len(with_short), total)})")
    print(f"  chars lost to normalisation: {len(dirty)} ({_pct(len(dirty), len(with_short))})")
    print(f"  disagrees with ticker prefix: {len(disagreeing)} ({_pct(len(disagreeing), total)})")
    print(f"  reused across listings:  {len(reused)} distinct symbols")
    if dirty:
        print(f"  sample dirty shortNames: {[i.short_name for i in dirty[:5]]}")
    if reused:
        print(f"  sample reused symbols:   {reused[:5]}")

    # --- 7: does workingScheduleId resolve to an exchange? ---------------
    with_schedule = [i for i in instruments if i.working_schedule_id is not None]
    resolvable = [i for i in with_schedule if i.working_schedule_id in schedule_to_exchange]
    unknown_ids = sorted(
        {
            schedule_id
            for i in with_schedule
            if (schedule_id := i.working_schedule_id) is not None
            and schedule_id not in schedule_to_exchange
        }
    )
    print(f"\nworkingScheduleId set:   {len(with_schedule)} ({_pct(len(with_schedule), total)})")
    print(
        f"  resolves to exchange:  {len(resolvable)} ({_pct(len(resolvable), len(with_schedule))})"
    )
    print(f"  unmapped schedule ids: {unknown_ids[:10]}")

    # Cross-check the derivation against the ticker's own venue code, which is
    # the thing the sync deliberately does *not* use as the exchange.
    agreements: Counter[str] = Counter()
    for instrument in resolvable:
        _symbol, market_code, _kind = split_broker_ticker(instrument.ticker)
        _exchange_id, exchange_name = schedule_to_exchange[instrument.working_schedule_id or -1]
        agreements[f"{market_code or '<none>'} -> {exchange_name or '<null>'}"] += 1
    print("\n  venue code -> derived exchange (top 15):")
    for pair, count in agreements.most_common(15):
        print(f"    {pair:44} {count}")

    # --- 8: rate-limit headers actually returned -------------------------
    print(f"\nexchanges rate limit:    {exchange_rate_limit}")
    print(f"instruments rate limit:  {instrument_rate_limit}")
    print(f"sample ticker:           {instruments[0].ticker}")

    # Trading 212 documents all five headers on every response. If they stop
    # arriving, the client's pacing is flying blind and that must be visible.
    assert exchange_rate_limit, "no x-ratelimit-* headers on the exchanges response"
    assert instrument_rate_limit, "no x-ratelimit-* headers on the instruments response"


async def test_live_alpaca_capability_and_quote() -> None:
    """Two GETs: the entitlement probe, then one quote for a liquid US equity."""
    settings = _alpaca_settings()
    client = AlpacaMarketDataClient(settings)
    try:
        capability = await client.capability(refresh=True)
        print(f"\nfeed configured:         {client.feed}")
        print(f"historical feed used:    {client.historical_feed()}")
        print(f"capability state:        {capability.state.value}")
        print(f"pricing usable:          {capability.realtime_pricing_usable}")
        print(f"detail:                  {capability.detail}")
        print(f"probe symbol:            {capability.probe_symbol}")
        print(f"probe quote age ms:      {capability.probe_quote_age_ms}")
        print(f"blockers:                {list(capability.blockers)}")

        if capability.state is not CapabilityState.HEALTHY:
            pytest.skip(f"alpaca capability is {capability.state.value}: {capability.detail}")

        quote = await client.latest_quote(settings.market_data_probe_symbol)
    finally:
        await client.aclose()

    blockers = quote_blockers(
        quote, capability, max_age_seconds=settings.market_data_max_quote_age_seconds
    )
    print(f"\nsymbol:                  {quote.symbol}")
    print(f"provider / feed:         {quote.provider} / {quote.feed}")
    print(f"price source:            {quote.price_source.value}")
    print(f"bid / ask:               {quote.bid} / {quote.ask}")
    print(f"bid size / ask size:     {quote.bid_size} / {quote.ask_size}")
    print(f"mid:                     {quote.price}")
    print(f"currency:                {quote.currency}")
    print(f"tape:                    {quote.tape}")
    print(f"conditions:              {list(quote.conditions)}")
    print(f"provider timestamp:      {quote.provider_timestamp.isoformat()}")
    print(f"received at:             {quote.received_at.isoformat()}")
    print(f"quote age ms:            {quote.age_ms}")
    print(f"two-sided:               {quote.is_two_sided}")
    print(f"sizing blockers:         {blockers}")

    assert isinstance(quote.price, Decimal)
    assert quote.age_ms >= 0
    # The feed must be the one that was asked for, not whatever the account
    # happened to default to.
    assert quote.feed == client.feed
