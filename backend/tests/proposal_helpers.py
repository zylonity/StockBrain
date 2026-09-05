"""Fixtures for database-backed proposal tests.

Seeds one boringly healthy world -- a resolved Apple listing, a succeeded
research run, a published BUY thesis and a funded account -- so that each test
breaks exactly one thing and the break is visible in the test body.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from stockbrain.broker.account_state import AccountStateService
from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import (
    BarTimeframe,
    Broker,
    CapabilityState,
    EventStatus,
    ImpactDirection,
    PriceSource,
    ResearchStatus,
    ResolutionStatus,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.errors import ProviderUnavailable
from stockbrain.market_data.base import Bar, ProviderCapability, Quote, Trade
from stockbrain.proposals.service import ProposalService
from stockbrain.risk.config import RiskConfig, risk_config_from_settings

COMPANY_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
INSTRUMENT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
EVENT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")
IMPACT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000004")
RUN_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000005")
THESIS_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000006")

ACCOUNT_ID = "4242"


class StubMarketData:
    """A market-data provider that answers exactly what a test asks it to.

    Deliberately not a mock library double: the risk engine's decisions hinge on
    the *shape* of a quote, so tests are clearer when they hand over a real
    :class:`Quote`.
    """

    name = "stub"

    def __init__(
        self,
        *,
        bid: Decimal = Decimal("199.95"),
        ask: Decimal = Decimal("200.05"),
        age_ms: int = 250,
        price_source: PriceSource = PriceSource.ALPACA_IEX,
        state: CapabilityState = CapabilityState.HEALTHY,
        usable: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.bid = bid
        self.ask = ask
        self.age_ms = age_ms
        self.price_source = price_source
        self.state = state
        self.usable = usable
        self.error = error
        self.quote_calls = 0

    async def latest_quote(self, symbol: str) -> Quote:
        self.quote_calls += 1
        if self.error is not None:
            raise self.error
        now = utcnow()
        return Quote(
            symbol=symbol,
            provider="stub",
            feed="iex",
            price_source=self.price_source,
            provider_timestamp=now - dt.timedelta(milliseconds=self.age_ms),
            received_at=now,
            bid=self.bid,
            ask=self.ask,
            bid_size=100,
            ask_size=100,
            currency="USD",
        )

    async def latest_trade(self, symbol: str) -> Trade:  # pragma: no cover - unused here
        raise ProviderUnavailable("stub has no trades")

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> Sequence[Bar]:  # pragma: no cover - unused here
        return []

    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        return ProviderCapability(
            provider="stub",
            state=self.state,
            feed="iex",
            realtime_pricing_usable=self.usable,
            checked_at=utcnow(),
        )


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "stockbrain_secret_key": "test-secret-key",
        "t212_api_key": "key",
        "t212_api_secret": "secret",
        # The seeded quote is a two-sided regular-hours book, but a test run at
        # 03:00 must not depend on the wall clock, so the session gate is
        # widened rather than the clock frozen.
        "risk_require_known_session": False,
        "risk_allowed_sessions": "REGULAR,PRE_MARKET,AFTER_HOURS,CLOSED,OVERNIGHT,UNKNOWN",
    }
    base.update(overrides)
    return Settings(**base)


def service_with(
    database: Database,
    resolved: Settings,
    *,
    market_data: StubMarketData | None = None,
    config: RiskConfig | None = None,
    control: ControlStateService | None = None,
) -> ProposalService:
    """Build a proposal service around an already-resolved ``Settings``.

    The Phase 7 tests need the *same* settings object for the service and for
    the Telegram side, so this takes one rather than building its own.
    """
    return ProposalService(
        database,
        resolved,
        risk_config=config or risk_config_from_settings(resolved),
        account_state=AccountStateService(
            database, None, broker=Broker.TRADING212, broker_environment=resolved.t212_env.value
        ),
        market_data=market_data or StubMarketData(),
        broker=Broker.TRADING212,
        control=control or ControlStateService(database),
    )


def service(
    database: Database,
    *,
    market_data: StubMarketData | None = None,
    config: RiskConfig | None = None,
    **setting_overrides: Any,
) -> ProposalService:
    return service_with(
        database,
        settings(**setting_overrides),
        market_data=market_data,
        config=config,
    )


async def seed(
    database: Database,
    *,
    action: ThesisAction = ThesisAction.BUY,
    confidence: float = 0.9,
    instrument_type: str = "STOCK",
    currency: str = "USD",
    is_active: bool = True,
    resolution: ResolutionStatus = ResolutionStatus.RESOLVED,
    thesis_id: uuid.UUID = THESIS_ID,
    run_id: uuid.UUID = RUN_ID,
) -> None:
    """Create the full research -> resolved-listing chain one thesis needs."""
    async with database.transaction() as session:
        session.add(Company(id=COMPANY_ID, name="Apple Inc.", isin="US0378331005"))
        session.add(
            Event(
                id=EVENT_ID,
                title="Apple announces something material",
                title_hash="c" * 64,
                summary="A material announcement.",
                first_seen_at=utcnow(),
                event_time=utcnow(),
                status=EventStatus.CANDIDATE,
            )
        )
        await session.flush()
        session.add(
            BrokerInstrument(
                id=INSTRUMENT_ID,
                company_id=COMPANY_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                market_symbol="AAPL",
                market_code="US",
                name="Apple Inc.",
                exchange="NASDAQ",
                currency=currency,
                isin="US0378331005",
                instrument_type=instrument_type,
                is_active=is_active,
                max_open_quantity=Decimal("55000"),
            )
        )
        await session.flush()
        session.add(
            EventCompanyImpact(
                id=IMPACT_ID,
                event_id=EVENT_ID,
                company_id=COMPANY_ID,
                company_name_hint="Apple",
                company_key="apple",
                ticker_hint="AAPL",
                direction=ImpactDirection.POSITIVE,
                materiality_score=0.8,
                confidence=0.85,
                impact_path="direct",
                broker_instrument_id=INSTRUMENT_ID,
                resolution_status=resolution,
            )
        )
        session.add(
            ResearchRun(
                id=run_id,
                event_id=EVENT_ID,
                company_id=COMPANY_ID,
                impact_id=IMPACT_ID,
                broker_instrument_id=INSTRUMENT_ID,
                status=ResearchStatus.SUCCEEDED,
                completed_at=utcnow(),
            )
        )
        await session.flush()
        session.add(
            Thesis(
                id=thesis_id,
                research_run_id=run_id,
                action=action,
                confidence=confidence,
                time_horizon=TimeHorizon.DAYS,
                summary="A thesis.",
            )
        )


async def fund(
    database: Database,
    *,
    cash: Decimal = Decimal("50000"),
    total: Decimal = Decimal("100000"),
    invested: Decimal = Decimal("20000"),
    currency: str = "USD",
    captured_at: dt.datetime | None = None,
    environment: str = "demo",
    positions: dict[str, tuple[Decimal, Decimal]] | None = None,
) -> None:
    """Persist a broker account snapshot and its open positions."""
    moment = captured_at or utcnow()
    async with database.transaction() as session:
        session.add(
            PortfolioSnapshot(
                broker=Broker.TRADING212,
                account_id=ACCOUNT_ID,
                captured_at=moment,
                currency=currency,
                cash_available=cash,
                cash_reserved=Decimal(0),
                cash_in_pies=Decimal(0),
                invested_value=invested,
                total_value=total,
                broker_environment=environment,
            )
        )
        for ticker, (quantity, available) in (positions or {}).items():
            session.add(
                Position(
                    broker=Broker.TRADING212,
                    account_id=ACCOUNT_ID,
                    broker_ticker=ticker,
                    quantity=quantity,
                    quantity_available=available,
                    average_price=Decimal("180"),
                    current_price=Decimal("200"),
                    currency=currency,
                    last_synced_at=moment,
                    raw={"wallet_impact": {"current_value": str(quantity * Decimal("200"))}},
                )
            )
