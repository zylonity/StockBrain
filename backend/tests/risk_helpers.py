"""Builders for deterministic-risk test inputs.

Every default here describes a boringly healthy world: a resolved, active US
equity, a fresh two-sided quote inside the spread ceiling during regular hours,
and an account with plenty of cash. Each test then breaks exactly one thing, so
what a test proves is visible in the line that overrides a default.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from stockbrain.enums import (
    Broker,
    MarketSession,
    PriceSource,
    ResolutionStatus,
    ThesisAction,
)
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import (
    AccountState,
    InstrumentIdentity,
    PositionState,
    QuoteSnapshot,
    ReservedExposure,
    RiskInputs,
)
from stockbrain.risk.spread import assess_spread

#: Distinguishes "use the healthy default" from "there deliberately isn't one".
#: ``None`` cannot do both jobs, and the tests that matter most here are exactly
#: the ones passing ``None`` on purpose.
UNSET: Any = object()

NOW = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
INSTRUMENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
COMPANY_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def config(**overrides: Any) -> RiskConfig:
    return RiskConfig(**overrides)


def identity(**overrides: Any) -> InstrumentIdentity:
    values: dict[str, Any] = {
        "broker_instrument_id": INSTRUMENT_ID,
        "broker": Broker.TRADING212,
        "broker_ticker": "AAPL_US_EQ",
        "market_symbol": "AAPL",
        "resolution_status": ResolutionStatus.RESOLVED,
        "is_active": True,
        "instrument_type": "STOCK",
        "currency": "USD",
        "exchange": "NASDAQ",
        "isin": "US0378331005",
        "company_id": COMPANY_ID,
        "max_open_quantity": Decimal("10000"),
        "extended_hours": True,
    }
    values.update(overrides)
    return InstrumentIdentity(**values)


def quote(
    *,
    bid: Decimal = Decimal("199.95"),
    ask: Decimal = Decimal("200.05"),
    age_ms: int = 500,
    max_spread_bps: Decimal = Decimal("50"),
    session: MarketSession = MarketSession.REGULAR,
    price_source: PriceSource = PriceSource.ALPACA_IEX,
    currency: str = "USD",
    provider_blockers: tuple[str, ...] = (),
    **overrides: Any,
) -> QuoteSnapshot:
    spread = assess_spread(bid, ask, max_spread_bps=max_spread_bps)
    values: dict[str, Any] = {
        "symbol": "AAPL",
        "provider": "alpaca",
        "feed": "iex",
        "price_source": price_source,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / Decimal(2) if bid and ask else None,
        "provider_timestamp": NOW - dt.timedelta(milliseconds=age_ms),
        "received_at": NOW,
        "age_ms": age_ms,
        "currency": currency,
        "spread": spread,
        "session": session,
        "session_source": "exchange_schedule",
        "session_holiday_aware": True,
        "provider_blockers": provider_blockers,
    }
    values.update(overrides)
    return QuoteSnapshot(**values)


def account(
    *,
    cash: Decimal = Decimal("10000"),
    total: Decimal = Decimal("20000"),
    invested: Decimal = Decimal("10000"),
    currency: str = "USD",
    positions: dict[str, PositionState] | None = None,
    captured_at: dt.datetime | None = None,
) -> AccountState:
    return AccountState(
        broker=Broker.TRADING212,
        account_id="12345",
        currency=currency,
        cash_available=cash,
        cash_reserved=Decimal(0),
        cash_in_pies=Decimal(0),
        invested_value=invested,
        total_value=total,
        captured_at=captured_at or NOW,
        positions=positions or {},
    )


def position(
    *,
    ticker: str = "AAPL_US_EQ",
    quantity: Decimal = Decimal("10"),
    available: Decimal | None = None,
    market_value: Decimal = Decimal("2000"),
) -> PositionState:
    return PositionState(
        broker_ticker=ticker,
        quantity=quantity,
        quantity_available=quantity if available is None else available,
        currency="USD",
        average_price=Decimal("180"),
        current_price=Decimal("200"),
        market_value=market_value,
    )


def inputs(
    *,
    action: ThesisAction = ThesisAction.BUY,
    confidence: Decimal = Decimal("0.9"),
    risk_config: RiskConfig | None = None,
    instrument: InstrumentIdentity | None = None,
    state: AccountState | None = UNSET,
    snapshot: QuoteSnapshot | None = UNSET,
    reserved: ReservedExposure | None = None,
    now: dt.datetime = NOW,
    account_state_missing_reason: str | None = None,
    quote_missing_reason: str | None = None,
) -> RiskInputs:
    return RiskInputs(
        config=risk_config or config(),
        action=action,
        confidence=confidence,
        identity=instrument or identity(),
        account=account() if state is UNSET else state,
        quote=quote() if snapshot is UNSET else snapshot,
        reserved=reserved or ReservedExposure(),
        now=now,
        account_state_missing_reason=account_state_missing_reason,
        quote_missing_reason=quote_missing_reason,
    )
