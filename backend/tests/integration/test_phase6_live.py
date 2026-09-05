"""Live, read-only verification of the Phase 6 account surface.

Run explicitly::

    pytest -m live -s tests/integration/test_phase6_live.py

Strictly read-only and strictly minimal: **three GETs at most** -- the Trading
212 account summary, its open positions, and one Alpaca quote. No order
endpoint is called, and the two Trading 212 clients reachable from here have no
order method to call. Nothing is written to the database.

**No balance, position size or account identifier is ever printed.** What
reaches stdout is the *contract*: which documented fields were present, their
Python types, whether every money value arrived as a ``Decimal``, whether the
instrument and account currencies agree, and the rate-limit headers. Those are
the questions unit tests cannot answer, because a unit test only knows what the
documentation says.

Reaching the **live** environment is gated behind two independent switches,
mirroring how live execution is gated, so it can never happen as a side effect
of running the suite:

* ``T212_ACCOUNT_ENV=live`` selects the environment, and
* ``T212_ALLOW_LIVE_ACCOUNT_READ=yes`` records that a human intended it.

Either one missing keeps the call on demo. Neither is written to ``.env``.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.broker.trading212_account import (
    ACCOUNT_SUMMARY_PATH,
    POSITIONS_PATH,
    Trading212AccountClient,
)
from stockbrain.config import Settings
from stockbrain.enums import CapabilityState
from stockbrain.market_data.alpaca import AlpacaMarketDataClient
from stockbrain.market_data.base import quote_blockers
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.spread import assess_spread

pytestmark = [pytest.mark.live, pytest.mark.integration]

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


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
        value = value.split(" #", 1)[0].strip().strip("'\"")
        return value
    return ""


def _account_settings() -> Settings:
    key = _env("T212_API_KEY")
    secret = _env("T212_API_SECRET")
    if not key or not secret:
        pytest.skip(
            "T212_API_KEY / T212_API_SECRET are not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )

    requested = (os.environ.get("T212_ACCOUNT_ENV") or "demo").strip().lower()
    consented = os.environ.get("T212_ALLOW_LIVE_ACCOUNT_READ", "").strip().lower() == "yes"
    if requested == "live" and not consented:
        pytest.skip(
            "T212_ACCOUNT_ENV=live requires T212_ALLOW_LIVE_ACCOUNT_READ=yes; "
            "refusing to read live account state implicitly"
        )
    environment = "live" if (requested == "live" and consented) else "demo"
    return Settings(
        app_env="test",
        t212_api_key=key,
        t212_api_secret=secret,
        t212_env=environment,
    )


def _report(title: str, rows: list[tuple[str, object]]) -> None:
    print(f"\n--- {title} ---")
    for label, value in rows:
        print(f"{label:<38} {value}")


async def test_account_summary_contract() -> None:
    """Two GETs: the account summary and the open positions.

    Asserts the shape the risk engine depends on, and prints only derived facts.
    """
    settings = _account_settings()
    client = Trading212AccountClient(settings)
    try:
        summary = await client.fetch_account_summary()
        summary_limits = client.rate_limit_snapshot()
        positions = await client.fetch_positions()
        position_limits = client.rate_limit_snapshot()
    finally:
        await client.aclose()

    # Every documented field is present and typed as the client expects.
    assert isinstance(summary.id, int)
    assert isinstance(summary.currency, str) and len(summary.currency) == 3
    for value in (
        summary.total_value,
        summary.cash.available_to_trade,
        summary.cash.in_pies,
        summary.cash.reserved_for_orders,
        summary.investments.current_value,
        summary.investments.realized_profit_loss,
        summary.investments.total_cost,
        summary.investments.unrealized_profit_loss,
    ):
        assert isinstance(value, Decimal), "a money value must never arrive as a float"

    foreign = [
        item
        for item in positions
        if (item.instrument.currency or "").upper() != summary.currency.upper()
    ]
    pies = [item for item in positions if item.quantity_in_pies > 0]
    partly_available = [
        item for item in positions if item.quantity_available_for_trading != item.quantity
    ]
    wallet_currencies = {
        (item.wallet_impact.currency or "").upper() for item in positions if item.wallet_impact
    }

    _report(
        f"Trading 212 account contract ({settings.t212_env.value})",
        [
            ("environment", settings.t212_env.value),
            ("GET " + ACCOUNT_SUMMARY_PATH, "200"),
            ("GET " + POSITIONS_PATH, "200"),
            ("account currency (ISO 4217)", summary.currency),
            ("account id type", type(summary.id).__name__),
            ("every money value is Decimal", True),
            ("open positions", len(positions)),
            ("positions in a foreign currency", len(foreign)),
            ("positions with shares inside a pie", len(pies)),
            ("positions where available != total", len(partly_available)),
            ("walletImpact currencies observed", sorted(wallet_currencies) or "n/a"),
            ("walletImpact matches account currency", wallet_currencies <= {summary.currency}),
            ("summary rate-limit headers", sorted(summary_limits) or "none"),
            ("positions rate-limit headers", sorted(position_limits) or "none"),
        ],
    )

    # The documented claim this phase relies on: every value is reported in the
    # primary account currency, so `walletImpact` needs no conversion of ours.
    if wallet_currencies:
        assert wallet_currencies <= {summary.currency.upper()}, (
            "walletImpact should be denominated in the primary account currency"
        )

    for item in positions:
        assert item.instrument.ticker, "a position must carry its broker ticker"
        assert item.quantity_available_for_trading <= item.quantity


async def test_the_account_client_has_no_order_method() -> None:
    """Stated as a live-suite assertion too, so it is checked where it matters."""
    surface = {name for name in dir(Trading212AccountClient) if not name.startswith("_")}
    assert surface == {
        "aclose",
        "fetch_account_summary",
        "fetch_positions",
        "name",
        "rate_limit_snapshot",
    }


async def test_one_alpaca_quote_against_the_phase_six_spread_ceiling() -> None:
    """One GET. Reports what the Phase 6 gates would actually decide right now."""
    key = _env("ALPACA_API_KEY")
    secret = _env("ALPACA_API_SECRET")
    if not key or not secret:
        pytest.skip(
            "ALPACA_API_KEY / ALPACA_API_SECRET are not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )
    settings = Settings(app_env="test", alpaca_api_key=key, alpaca_api_secret=secret)
    config = RiskConfig()

    client = AlpacaMarketDataClient(settings)
    try:
        capability = await client.capability(refresh=True)
        quote = await client.latest_quote(settings.market_data_probe_symbol)
    finally:
        await client.aclose()

    spread = assess_spread(quote.bid, quote.ask, max_spread_bps=config.max_spread_bps)
    blockers = quote_blockers(
        quote,
        capability,
        max_age_seconds=float(config.max_quote_age_seconds),
        max_spread_bps=config.max_spread_bps,
    )

    _report(
        f"Alpaca quote against the Phase 6 gates ({settings.market_data_probe_symbol})",
        [
            ("capability state", capability.state.value),
            ("feed", quote.feed),
            ("price source", quote.price_source.value),
            ("bid / ask", f"{quote.bid} / {quote.ask}"),
            ("mid", quote.price),
            ("spread", spread.spread),
            ("spread (bps)", spread.spread_bps),
            ("spread status", spread.status.value),
            ("ceiling (bps)", config.max_spread_bps),
            ("quote age (ms)", quote.age_ms),
            ("age limit (s)", config.max_quote_age_seconds),
            ("sizing blockers", blockers or "none -- this quote could size an order"),
        ],
    )

    assert capability.state is not CapabilityState.AUTH_FAILED
    # Whatever the market is doing, the two facts must be consistent: a quote
    # with no blockers must have an OK spread, and a refused spread must appear
    # in the blocker list.
    if not blockers:
        assert spread.is_ok
    if not spread.is_ok:
        assert any(spread.detail in item or item == spread.detail for item in blockers)
