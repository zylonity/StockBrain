"""Live, opt-in verification of the Trading 212 execution surface.

Run explicitly::

    pytest -m live -s tests/integration/test_phase8_live.py

**Demo only, and never by accident.**  Three separate switches stand between
this file and a real order, and the last one is the only thing in the repository
that can place one:

* credentials must be configured, and they must authenticate against
  ``demo.trading212.com``;
* ``T212_DEMO_ORDER=yes`` records that a human intended a broker mutation;
* ``T212_DEMO_ORDER_TICKER`` names the instrument, because the test never picks
  one -- choosing an instrument on somebody's behalf is not a thing a test does.

Without the second switch the file performs **read-only** verification: it
proves the order endpoints authenticate and reports their documented shapes.
A *live-environment* order is impossible from here regardless of switches: the
environment is pinned to demo below, and the client refuses a mismatch.

Nothing account-sensitive is printed -- no balance, no position size, no account
id, no order id.  What reaches stdout is the *contract*: which documented fields
were present, their Python types, whether every money value arrived as a
``Decimal``, and the rate-limit headers.  Those are the questions unit tests
cannot answer, because a unit test only knows what the documentation says.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.broker.trading212_orders import (
    HISTORY_ORDERS_PATH,
    MARKET_ORDER_PATH,
    MARKET_ORDER_RATE_PER_MINUTE,
    ORDERS_PATH,
    Trading212OrderClient,
)
from stockbrain.config import Settings
from stockbrain.errors import ProviderAuthError

pytestmark = [pytest.mark.live, pytest.mark.integration]

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _env(name: str) -> str:
    """Read one setting from the process environment or the root ``.env``.

    Returned, never logged.  Only *presence* is ever reported, and only as a
    skip reason.
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
        return value.split(" #", 1)[0].strip().strip("'\"")
    return ""


def _demo_settings() -> Settings:
    """Settings pinned to demo, whatever ``.env`` says.

    ``T212_ENV`` is not read from configuration here. A live order from a test
    run must be impossible rather than merely discouraged.
    """
    key = _env("T212_API_KEY")
    secret = _env("T212_API_SECRET")
    if not key or not secret:
        pytest.skip(
            "T212_API_KEY / T212_API_SECRET are not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )
    return Settings(
        app_env="test",
        stockbrain_secret_key="live-test",
        t212_api_key=key,
        t212_api_secret=secret,
        t212_env="demo",
        t212_execution_enabled=True,
    )


async def test_the_order_endpoints_authenticate_against_demo() -> None:
    """Two read-only GETs.  No order is placed.

    A Practice/Demo API key is required: Trading 212 issues keys per
    environment, and a live key returns HTTP 401 against ``demo.trading212.com``
    -- which is itself the finding this test reports when it happens.
    """
    settings = _demo_settings()
    client = Trading212OrderClient(settings)
    try:
        try:
            pending = await client.fetch_pending_orders()
        except ProviderAuthError:
            pytest.skip(
                "the configured Trading 212 key does not authenticate against the DEMO "
                "environment (Trading 212 issues keys per environment; generate a "
                "Practice/Demo key in the app). No order was placed."
            )
        pending_limits = client.rate_limit_snapshot()
        history = await client.fetch_order_history(limit=5)
        history_limits = client.rate_limit_snapshot()
    finally:
        await client.aclose()

    print("\n--- Trading 212 order endpoints (demo, read-only) ---")
    print(f"environment            : {client.environment}")
    print(f"{ORDERS_PATH} authenticated : True")
    print(f"pending orders returned: {len(pending)}")
    print(f"pending rate-limit hdrs: {sorted(pending_limits)}")
    print(f"{HISTORY_ORDERS_PATH} authenticated : True")
    print(f"history items returned : {len(history)}")
    print(f"history rate-limit hdrs: {sorted(history_limits)}")
    print(f"order endpoint         : {MARKET_ORDER_PATH}")
    print(f"documented order limit : {MARKET_ORDER_RATE_PER_MINUTE} req/min")

    assert client.environment == "demo"
    # Every documented rate-limit header, on both endpoints.
    for snapshot in (pending_limits, history_limits):
        assert snapshot.get("limit") is not None
        assert snapshot.get("remaining") is not None
        assert snapshot.get("period") is not None

    if history:
        order = history[0]
        print("--- one historical order, as a contract ---")
        print(f"has id                 : {order.id > 0}")
        print(f"has ticker             : {order.broker_ticker is not None}")
        print(f"side                   : {order.side}")
        print(f"status                 : {order.status}")
        print(f"type                   : {order.type}")
        print(f"initiatedFrom          : {order.initiated_from}")
        print(f"quantity is Decimal    : {isinstance(order.quantity, Decimal)}")
        print(
            "filledQuantity Decimal : "
            f"{order.filled_quantity is None or isinstance(order.filled_quantity, Decimal)}"
        )
        assert order.quantity is None or isinstance(order.quantity, Decimal)


async def test_a_single_smallest_demo_order_can_be_placed_and_reconciled() -> None:
    """The end-to-end broker mutation, behind two more explicit switches.

    Deliberately not driven through the proposal pipeline: the risk engine
    currently blocks this account's entire priced universe on
    ``currency_alignment`` (a GBP account against USD listings, measured in
    Phase 6), so a pipeline run would refuse before reaching the broker and
    prove nothing about the adapter.  What is verified here is the part only a
    live call can verify: the request shape, the response shape, the sign
    convention and the rate-limit headers.

    One order, the smallest quantity the caller names, on the instrument the
    caller names.  It is **not** cancelled afterwards -- StockBrain has no
    cancel path and this test does not add one -- so the operator should expect
    a small demo position and close it in the app if they want to.
    """
    if os.environ.get("T212_DEMO_ORDER", "").strip().lower() != "yes":
        pytest.skip("T212_DEMO_ORDER=yes is required before this test places a demo order")
    ticker = os.environ.get("T212_DEMO_ORDER_TICKER", "").strip()
    if not ticker:
        pytest.skip("T212_DEMO_ORDER_TICKER must name the instrument; this test never picks one")
    raw_quantity = os.environ.get("T212_DEMO_ORDER_QUANTITY", "1").strip()
    quantity = Decimal(raw_quantity)
    assert quantity > 0, "the demo order quantity must be positive"

    settings = _demo_settings()
    client = Trading212OrderClient(settings)
    try:
        assert await client.reserve_order_slot() is True
        response = await client.submit_market_order(
            broker_ticker=ticker,
            signed_quantity=quantity,
            extended_hours=False,
            broker_environment="demo",
        )
        limits = client.rate_limit_snapshot()
        # Read it back, exactly as reconciliation would.
        looked_up = await client.fetch_order(str(response.order.id))
    finally:
        await client.aclose()

    order = response.order
    print("\n--- Trading 212 demo market order (ONE order placed) ---")
    print("environment            : demo")
    print(f"http status            : {response.http_status}")
    print(f"has order id           : {order.id > 0}")
    print(f"ticker echoed          : {order.broker_ticker == ticker}")
    print(f"side                   : {order.side}")
    print(f"status                 : {order.status}")
    print(f"type                   : {order.type}")
    print(f"strategy               : {order.strategy}")
    print(f"timeInForce            : {order.time_in_force}")
    print(f"initiatedFrom          : {order.initiated_from}")
    print(f"quantity is Decimal    : {isinstance(order.quantity, Decimal)}")
    print(f"quantity sign matches  : {(order.quantity or Decimal(0)) > 0}")
    print(f"instrument object      : {order.instrument is not None}")
    print(f"rate-limit headers     : {sorted(limits)}")
    print(f"readable by id         : {looked_up is not None}")

    assert response.http_status == 200
    assert order.id > 0
    assert order.type == "MARKET"
    assert order.side == "BUY"
    # The provenance reconciliation depends on.
    assert order.initiated_from == "API"
    assert order.placed_by_api
    assert isinstance(order.quantity, Decimal)
    assert limits.get("limit") is not None
