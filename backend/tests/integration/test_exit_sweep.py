"""Exit sweep: peak tracking, origin-thesis recovery and proposal generation."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.broker.account_state import AccountStateService
from stockbrain.broker.trading212_account import (
    T212AccountSummary,
    T212Position,
    Trading212AccountClient,
)
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.portfolio import Position, PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    ImpactDirection,
    JobType,
    NotificationEvent,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    ResearchStatus,
    ResolutionStatus,
    RuleOutcome,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.proposals.exits import ExitSweepService
from stockbrain.proposals.rebalance import RebalanceService
from stockbrain.proposals.service import GenerationResult, ProposalService
from stockbrain.proposals.state_machine import ACTIVE_STATUSES
from stockbrain.risk.config import risk_config_from_settings
from stockbrain.risk.exits import ExitSignal
from stockbrain.risk.models import RuleResult
from tests import proposal_helpers as ph

# The documented payload shape, mirroring the double in
# ``tests/unit/test_trading212_account.py``.  Only the fields ``sync`` reads are
# present, and they are nested under ``instrument``/``walletImpact`` because
# that is the shape Trading 212 currently returns.
_SUMMARY = {
    "id": 4242,
    "currency": "GBP",
    "totalValue": 9234.81,
    "cash": {"availableToTrade": 1234.56, "reservedForOrders": 0.0, "inPies": 0.0},
    "investments": {"currentValue": 8000.25, "unrealizedProfitLoss": 500.25},
}

_POSITION = {
    "instrument": {"ticker": "AAPL_US_EQ", "currency": "USD"},
    "quantity": 12.5,
    "quantityAvailableForTrading": 12.5,
    "quantityInPies": 0.0,
    "averagePricePaid": 180.5,
    "currentPrice": 200.25,
    "createdAt": dt.datetime(2026, 1, 5, 9, 30, tzinfo=dt.UTC),
    "walletImpact": {"currency": "GBP", "currentValue": 1975.0, "unrealizedProfitLoss": 175.0},
}


class _FakeAccountClient(Trading212AccountClient):
    """The real client with both reads answered from memory.

    Modelled on the double in ``tests/unit/test_trading212_account.py``: the
    documented payloads are parsed through the real pydantic models, so ``sync``
    sees exactly the fields the broker would hand it.  ``prices`` and
    ``positions`` are mutable so a test can move the market between syncs.
    """

    def __init__(self, *, prices: list[Decimal]) -> None:
        self.prices = prices
        self.positions: list[dict[str, object]] = [dict(_POSITION)]

    async def fetch_account_summary(self) -> T212AccountSummary:
        return T212AccountSummary.model_validate(_SUMMARY)

    async def fetch_positions(self) -> list[T212Position]:
        return [
            T212Position.model_validate({**payload, "currentPrice": self.prices[index]})
            for index, payload in enumerate(self.positions)
        ]


class _AccountService(AccountStateService):
    """The real service wired to :class:`_FakeAccountClient`.

    ``client`` is public here so a test can change the fake between syncs.
    """

    def __init__(self, database: Database, *, prices: list[Decimal]) -> None:
        self.client = _FakeAccountClient(prices=prices)
        super().__init__(
            database,
            self.client,
            broker=Broker.TRADING212,
            broker_environment="demo",
        )


async def _account_service(database: Database, *, prices: list[Decimal]) -> _AccountService:
    return _AccountService(database, prices=prices)


async def test_the_peak_ratchets_up_and_never_down(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.prices = [Decimal("130")]
    await service.sync()

    service.client.prices = [Decimal("110")]
    await service.sync()

    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.peak_price == Decimal("130")
    assert peak.observations == 3


async def test_closing_a_position_deletes_its_peak(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.positions = []
    await service.sync()

    async with database.session() as session:
        remaining = (
            await session.execute(sa.select(sa.func.count()).select_from(PositionPeak))
        ).scalar_one()
    assert remaining == 0


async def _proposal_service(database: Database) -> ProposalService:
    """The real service over the shared stub provider, as the proposal tests build it."""
    return ph.service(database)


async def _exit_sweep(database: Database) -> ExitSweepService:
    """The real sweep over the shared stub provider.

    The proposal service and the sweep share one ``Settings`` and one
    ``RiskConfig``, exactly as the container wires them.
    """
    resolved = ph.settings()
    config = risk_config_from_settings(resolved)
    proposals = ph.service_with(database, resolved, config=config)
    return ExitSweepService(database, resolved, proposals=proposals, config=config)


def _rule(rule_id: str) -> RuleResult:
    """The ``RuleResult`` a fired exit rule carries, in the shape Task 3 emits."""
    return RuleResult(
        rule_id=rule_id,
        rule_version=1,
        outcome=RuleOutcome.WARN,
        reason="the exit rule fired",
        observed="observed",
        threshold="threshold",
    )


@dataclass(frozen=True, slots=True)
class _ExecutedBuy:
    """The lineage a position StockBrain opened leaves behind."""

    thesis_id: uuid.UUID
    research_run_id: uuid.UUID
    proposal_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Listing:
    """An independent company -> listing -> research -> thesis chain."""

    thesis_id: uuid.UUID
    research_run_id: uuid.UUID


async def _seed_listing(
    database: Database, *, broker_ticker: str, market_symbol: str, name: str
) -> _Listing:
    """A second exitable listing beyond ``proposal_helpers``' one Apple world.

    The sweep's starvation regression needs several positions that each signal
    and each produce their own proposal, and a proposal's ticker comes from the
    listing its thesis resolved to -- so each position needs its own listing.
    """
    company_id, instrument_id, impact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run_id, thesis_id = uuid.uuid4(), uuid.uuid4()
    isin = f"TEST{market_symbol}"
    async with database.transaction() as session:
        session.add(Company(id=company_id, name=name, isin=isin))
        await session.flush()
        session.add(
            BrokerInstrument(
                id=instrument_id,
                company_id=company_id,
                broker=Broker.TRADING212,
                broker_ticker=broker_ticker,
                market_symbol=market_symbol,
                market_code="US",
                name=name,
                exchange="NASDAQ",
                currency="USD",
                isin=isin,
                instrument_type="STOCK",
                is_active=True,
                max_open_quantity=Decimal("55000"),
            )
        )
        await session.flush()
        session.add(
            EventCompanyImpact(
                id=impact_id,
                event_id=ph.EVENT_ID,
                company_id=company_id,
                company_name_hint=name,
                company_key=market_symbol.lower(),
                ticker_hint=market_symbol,
                direction=ImpactDirection.POSITIVE,
                materiality_score=0.8,
                confidence=0.85,
                impact_path="direct",
                broker_instrument_id=instrument_id,
                resolution_status=ResolutionStatus.RESOLVED,
            )
        )
        session.add(
            ResearchRun(
                id=run_id,
                event_id=ph.EVENT_ID,
                company_id=company_id,
                impact_id=impact_id,
                broker_instrument_id=instrument_id,
                status=ResearchStatus.SUCCEEDED,
                completed_at=utcnow(),
            )
        )
        await session.flush()
        session.add(
            Thesis(
                id=thesis_id,
                research_run_id=run_id,
                action=ThesisAction.BUY,
                confidence=0.9,
                time_horizon=TimeHorizon.DAYS,
                summary="A thesis.",
            )
        )
    return _Listing(thesis_id=thesis_id, research_run_id=run_id)


async def _seed_executed_buy(
    database: Database,
    *,
    broker_ticker: str = "AAPL_US_EQ",
    quantity: Decimal = Decimal("9"),
    dedupe_key: str | None = None,
    listing: _Listing | None = None,
) -> _ExecutedBuy:
    """Seed a funded, held position and the executed BUY proposal that opened it.

    The research chain is ``proposal_helpers``' healthy world unless ``listing``
    supplies an independent one (seeded by :func:`_seed_listing`); the final
    proposal is written directly.  ``dedupe_key`` defaults to ``None`` so the
    inherited-thesis test starts from a clean slate; the coexistence regression
    passes the key the ordinary generation path would have stored.
    """
    if listing is None:
        await ph.seed(database)
        thesis_id, run_id = ph.THESIS_ID, ph.RUN_ID
    else:
        thesis_id, run_id = listing.thesis_id, listing.research_run_id
    await ph.fund(database, positions={broker_ticker: (quantity, quantity)})
    moment = utcnow()
    proposal = TradeProposal(
        thesis_id=thesis_id,
        research_run_id=run_id,
        broker=Broker.TRADING212,
        broker_ticker=broker_ticker,
        account_id=ph.ACCOUNT_ID,
        broker_environment=ph.settings().t212_env.value,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=quantity,
        reference_price=Decimal("180.50"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=moment,
        quote_age_ms=0,
        estimated_notional=quantity * Decimal("180.50"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=moment,
        expires_at=moment + dt.timedelta(days=1),
        dedupe_key=dedupe_key,
    )
    async with database.transaction() as session:
        session.add(proposal)
    return _ExecutedBuy(
        thesis_id=thesis_id,
        research_run_id=run_id,
        proposal_id=proposal.id,
    )


async def _seed_position(
    database: Database,
    *,
    broker_ticker: str,
    average_price: Decimal,
    current_price: Decimal,
) -> None:
    """Reprice the funded holding ``_seed_executed_buy`` left behind.

    The broker's own price is what the sweep reads; the market-data quote is a
    separate path and is untouched.
    """
    async with database.transaction() as session:
        position = (
            await session.execute(
                sa.select(Position).where(
                    Position.broker == Broker.TRADING212,
                    Position.broker_ticker == broker_ticker,
                )
            )
        ).scalar_one()
        position.average_price = average_price
        position.current_price = current_price


async def _seed_live_proposal(database: Database, *, broker_ticker: str) -> None:
    """One non-terminal proposal already occupies the listing.

    The database's one-active-proposal-per-listing index is the invariant the
    sweep must respect without spending a quote to discover it.
    """
    moment = utcnow()
    proposal = TradeProposal(
        broker=Broker.TRADING212,
        broker_ticker=broker_ticker,
        account_id=ph.ACCOUNT_ID,
        broker_environment=ph.settings().t212_env.value,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("1"),
        reference_price=Decimal("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=moment,
        quote_age_ms=0,
        estimated_notional=Decimal("100"),
        account_currency="USD",
        status=ProposalStatus.READY,
        expires_at=moment + dt.timedelta(days=1),
    )
    async with database.transaction() as session:
        session.add(proposal)


async def test_an_exit_proposal_inherits_the_origin_thesis(clean_tables: Database) -> None:
    """Section 17's lineage requirement, satisfied without a schema change: the
    exit proposal points at the thesis the position was opened on."""
    database = clean_tables
    fixture = await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")

    service = await _proposal_service(database)
    signal = ExitSignal(
        rule_id="hard_stop",
        action=ThesisAction.SELL,
        reason="the position is -9.00% against average cost",
        rule=_rule("hard_stop"),
    )
    result = await service.generate_exit("AAPL_US_EQ", signal)

    assert result.created
    async with database.session() as session:
        proposal = await session.get(TradeProposal, result.proposal_id)
    assert proposal is not None
    assert proposal.thesis_id == fixture.thesis_id
    assert proposal.research_run_id == fixture.research_run_id
    assert proposal.side is OrderSide.SELL
    assert {item["reason"] for item in proposal.sizing_reasons} >= {signal.reason}
    assert any(rule["rule_id"] == "hard_stop" for rule in proposal.risk_rules)


async def test_an_exit_proposal_coexists_with_its_origin_buy(clean_tables: Database) -> None:
    """A real executed buy keeps its dedupe key, and its exit is still created.

    The ordinary generation path stamps the opening BUY with the key
    ``_dedupe_key`` derives, and ``dedupe_key`` is a plain UNIQUE column.  An
    exit that reused the origin thesis would compute the identical key and be
    swallowed as a duplicate -- created=False against a position that plainly
    needs exiting.  This seeds the buy the way production leaves it and asserts
    the exit proposal is still written.
    """
    database = clean_tables
    service = await _proposal_service(database)
    fixture = await _seed_executed_buy(
        database,
        broker_ticker="AAPL_US_EQ",
        dedupe_key=service._dedupe_key(ph.THESIS_ID, "AAPL_US_EQ"),
    )

    async with database.session() as session:
        origin = await session.get(TradeProposal, fixture.proposal_id)
    assert origin is not None
    assert origin.dedupe_key == service._dedupe_key(ph.THESIS_ID, "AAPL_US_EQ")

    result = await service.generate_exit(
        "AAPL_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="the position is -9.00% against average cost",
            rule=_rule("hard_stop"),
        ),
    )

    assert result.created, result.reason
    assert result.proposal_id != fixture.proposal_id


async def test_a_position_with_no_origin_proposal_is_not_exited(clean_tables: Database) -> None:
    database = clean_tables
    service = await _proposal_service(database)
    result = await service.generate_exit(
        "MSFT_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="irrelevant",
            rule=_rule("hard_stop"),
        ),
    )
    assert not result.created
    assert result.reason is not None
    assert "no executed StockBrain buy" in result.reason


async def test_the_sweep_proposes_an_exit_for_a_losing_position(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )

    sweep = await _exit_sweep(database)
    counts = await sweep.sweep()

    assert counts["proposed"] == 1
    assert counts["signalled"] == 1
    async with database.session() as session:
        proposal = (
            await session.execute(
                sa.select(TradeProposal).where(TradeProposal.side == OrderSide.SELL)
            )
        ).scalar_one()
    assert any(rule["rule_id"] == "hard_stop" for rule in proposal.risk_rules)


async def test_the_sweep_holds_a_healthy_position(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("101"),
    )

    counts = await (await _exit_sweep(database)).sweep()

    assert counts["signalled"] == 0
    assert counts["proposed"] == 0


async def test_the_sweep_skips_a_position_that_already_has_a_live_proposal(
    clean_tables: Database,
) -> None:
    """One active proposal per listing is a database invariant; the sweep must
    not spend a quote request discovering it."""
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )
    await _seed_live_proposal(database, broker_ticker="AAPL_US_EQ")

    counts = await (await _exit_sweep(database)).sweep()

    assert counts["skipped_active_proposal"] == 1
    assert counts["proposed"] == 0


async def test_the_sweep_defers_positions_beyond_its_per_tick_budget(
    clean_tables: Database,
) -> None:
    """A stable order plus a query LIMIT would starve every later position.

    Every open position must be evaluated on every tick; ``limit`` is a budget
    on how many proposals one tick may generate, not a bound on which positions
    are looked at.  The deferred position must be the one proposed next tick.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    for market_symbol in ("MSFT", "GOOG"):
        broker_ticker = f"{market_symbol}_US_EQ"
        listing = await _seed_listing(
            database, broker_ticker=broker_ticker, market_symbol=market_symbol, name=market_symbol
        )
        await _seed_executed_buy(database, broker_ticker=broker_ticker, listing=listing)
    for broker_ticker in ("AAPL_US_EQ", "GOOG_US_EQ", "MSFT_US_EQ"):
        await _seed_position(
            database,
            broker_ticker=broker_ticker,
            average_price=Decimal("100"),
            current_price=Decimal("88"),
        )

    sweep = await _exit_sweep(database)

    first = await sweep.sweep(limit=2)
    assert first["signalled"] == 3
    assert first["proposed"] == 2
    assert first["deferred"] == 1

    second = await sweep.sweep(limit=2)
    assert second["skipped_active_proposal"] == 2
    assert second["proposed"] == 1
    assert second["deferred"] == 0


async def test_a_superseded_thesis_exit_survives_the_precondition_sweep(
    clean_tables: Database,
) -> None:
    """A ``thesis_superseded`` exit must not be invalidated by the same sweep.

    The exit proposal reuses the origin thesis, and that thesis is superseded --
    that is *why* the exit exists.  Invalidating every active proposal on a
    superseded thesis would cancel the exit within one proposal-sweep tick and
    re-create it on the next, forever.  A superseded thesis says "do not enter
    on this"; for a reduction it is the reason to act.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("100"),
    )
    async with database.transaction() as session:
        session.add(
            Thesis(
                id=uuid.uuid4(),
                research_run_id=ph.RUN_ID,
                action=ThesisAction.SELL,
                confidence=0.8,
                time_horizon=TimeHorizon.DAYS,
                supersedes_thesis_id=ph.THESIS_ID,
            )
        )

    resolved = ph.settings()
    config = risk_config_from_settings(resolved)
    proposals = ph.service_with(database, resolved, config=config)
    sweep = ExitSweepService(database, resolved, proposals=proposals, config=config)

    counts = await sweep.sweep()
    assert counts["proposed"] == 1

    async with database.session() as session:
        proposal = (
            await session.execute(
                sa.select(TradeProposal).where(TradeProposal.side == OrderSide.SELL)
            )
        ).scalar_one()
        proposal_id = proposal.id
        assert any(rule["rule_id"] == "thesis_superseded" for rule in proposal.risk_rules)

    await proposals.sweep()

    async with database.session() as session:
        refreshed = await session.get(TradeProposal, proposal_id)
    assert refreshed is not None
    assert refreshed.status in ACTIVE_STATUSES
    assert refreshed.invalidated_at is None


async def test_an_exit_under_automatic_policy_is_still_notified(
    clean_tables: Database,
) -> None:
    """An exit is not auto-authorized unless EXECUTION_AUTO_AUTHORIZE_EXITS opts in.

    ``generate_exit`` never runs the automatic-authorization tail, so under
    ``ExecutionPolicy.AUTOMATIC`` the proposal is created READY and never
    announced -- an exit no human is told about.  Under either policy a human
    must see it, so the manual notification is unconditional.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    resolved = ph.settings(execution_policy="automatic")
    service = ph.service_with(database, resolved)

    result = await service.generate_exit(
        "AAPL_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="the position is -9.00% against average cost",
            rule=_rule("hard_stop"),
        ),
    )
    assert result.created, result.reason

    async with database.session() as session:
        jobs = list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.SEND_NOTIFICATION.value)
                )
            ).scalars()
        )
    assert any(
        job.payload["proposal_id"] == str(result.proposal_id)
        and job.payload["event"] == NotificationEvent.PROPOSAL_MANUAL.value
        for job in jobs
    )


async def test_an_exit_is_auto_authorized_when_exits_are_opted_in(
    clean_tables: Database,
) -> None:
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    resolved = ph.settings(execution_policy="automatic", execution_auto_authorize_exits=True)
    assert resolved.automatic_authorization_permitted, resolved.automation_blockers
    service = ph.service_with(database, resolved)

    result = await service.generate_exit(
        "AAPL_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="the position is -9.00% against average cost",
            rule=_rule("hard_stop"),
        ),
    )
    assert result.created, result.reason
    assert result.authorized

    async with database.session() as session:
        proposal = await session.get(TradeProposal, result.proposal_id)
        jobs = list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.SEND_NOTIFICATION.value)
                )
            ).scalars()
        )
    assert proposal is not None and proposal.approved_by == "system:automatic"
    events = {job.payload["event"] for job in jobs}
    assert NotificationEvent.PROPOSAL_AUTO_AUTHORIZED.value in events
    assert NotificationEvent.PROPOSAL_MANUAL.value not in events


async def test_the_sweep_isolates_one_positions_failure(
    clean_tables: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One position whose exit generation raises must not abort the tick.

    The sweep evaluates every open position; an unexpected exception from
    ``evaluate_exit`` or ``generate_exit`` for one holding has to be counted and
    logged, and the remaining holdings still proposed.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    listing = await _seed_listing(
        database,
        broker_ticker="MSFT_US_EQ",
        market_symbol="MSFT",
        name="Microsoft",
    )
    await _seed_executed_buy(database, broker_ticker="MSFT_US_EQ", listing=listing)
    for broker_ticker in ("AAPL_US_EQ", "MSFT_US_EQ"):
        await _seed_position(
            database,
            broker_ticker=broker_ticker,
            average_price=Decimal("100"),
            current_price=Decimal("88"),
        )

    resolved = ph.settings()
    config = risk_config_from_settings(resolved)
    proposals = ph.service_with(database, resolved, config=config)
    sweep = ExitSweepService(database, resolved, proposals=proposals, config=config)

    original = proposals.generate_exit

    async def flaky(
        broker_ticker: str,
        signal: ExitSignal,
        *,
        now: dt.datetime | None = None,
    ) -> GenerationResult:
        if broker_ticker == "AAPL_US_EQ":
            raise RuntimeError("boom")
        return await original(broker_ticker, signal, now=now)

    monkeypatch.setattr(proposals, "generate_exit", flaky)

    counts = await sweep.sweep()

    assert counts["failed"] == 1
    assert counts["proposed"] == 1
    async with database.session() as session:
        proposed = (
            await session.execute(
                sa.select(TradeProposal).where(TradeProposal.side == OrderSide.SELL)
            )
        ).scalar_one()
    assert proposed.broker_ticker == "MSFT_US_EQ"


async def _seed_peak(
    database: Database,
    *,
    broker_ticker: str,
    peak_price: Decimal,
    observations: int = 3,
) -> None:
    """The high-water mark the volatility and trailing rules read."""
    async with database.transaction() as session:
        session.add(
            PositionPeak(
                broker=Broker.TRADING212,
                account_id=ph.ACCOUNT_ID,
                broker_ticker=broker_ticker,
                peak_price=peak_price,
                peak_at=utcnow(),
                observations=observations,
            )
        )


async def test_status_reports_a_managed_positions_floors(clean_tables: Database) -> None:
    """The operator sees the same hard floor the exit rule would act on.

    ``status`` shares the observation builder with ``sweep``, so the number here
    is computed by the very predicate that fires the rule -- it cannot drift.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("101"),
    )
    await _seed_peak(database, broker_ticker="AAPL_US_EQ", peak_price=Decimal("105"))

    async with database.session() as session:
        statuses = await (await _exit_sweep(database)).status(session)

    status = statuses["AAPL_US_EQ"]
    assert status.managed is True
    assert status.reason is None
    assert status.floors is not None
    assert status.floors.hard_stop == Decimal("100") * Decimal("0.92")
    assert status.peak_price == Decimal("105")
    assert status.horizon == TimeHorizon.DAYS.value


async def test_status_reports_an_unmanaged_position_without_inventing_floors(
    clean_tables: Database,
) -> None:
    """A position StockBrain never opened has no thesis, so no floors."""
    database = clean_tables
    await ph.fund(database, positions={"AAPL_US_EQ": (Decimal("9"), Decimal("9"))})

    async with database.session() as session:
        statuses = await (await _exit_sweep(database)).status(session)

    status = statuses["AAPL_US_EQ"]
    assert status.managed is False
    assert status.reason is not None
    assert "no StockBrain buy" in status.reason
    assert status.floors is None


async def test_status_reports_floors_even_while_a_proposal_is_pending(
    clean_tables: Database,
) -> None:
    """The sweep skips a listing with a live proposal; the operator must not.

    An exit already waiting for approval is exactly when its floors matter most,
    so ``status`` never applies the sweep's active-proposal skip.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )
    await _seed_live_proposal(database, broker_ticker="AAPL_US_EQ")

    async with database.session() as session:
        statuses = await (await _exit_sweep(database)).status(session)

    status = statuses["AAPL_US_EQ"]
    assert status.managed is True
    assert status.floors is not None
    assert status.floors.hard_stop == Decimal("92")


async def test_status_reports_no_floors_for_a_position_with_no_tradable_shares(
    clean_tables: Database,
) -> None:
    """A pie-held or unsettled position has floors the sweep would never act on.

    It is still managed -- there is a thesis and a buy behind it -- but the
    rules refuse to signal without tradeable shares, so no floor is shown and
    the reason says why.
    """
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )
    async with database.transaction() as session:
        position = (
            await session.execute(sa.select(Position).where(Position.broker_ticker == "AAPL_US_EQ"))
        ).scalar_one()
        position.quantity_available = Decimal("0")

    async with database.session() as session:
        statuses = await (await _exit_sweep(database)).status(session)

    status = statuses["AAPL_US_EQ"]
    assert status.managed is True
    assert status.floors is None
    assert status.reason is not None
    assert "no tradable shares" in status.reason


def _rebalancer(database: Database, **overrides: object) -> RebalanceService:
    resolved = ph.settings(risk_sizing_mode="conviction", risk_target_positions=0, **overrides)
    service = ph.service_with(database, resolved)
    return RebalanceService(service, service.account_state)


async def test_rebalance_preview_trades_nothing(clean_tables: Database) -> None:
    await _seed_executed_buy(clean_tables, broker_ticker="AAPL_US_EQ")
    plan = await _rebalancer(clean_tables, risk_max_position_pct="0.01").plan()
    assert plan.unavailable is None
    [line] = plan.lines
    # 9 shares at 200 = 1800 held; one holding capped at 1% of 100000 = 1000.
    assert line.action == "TRIM" and line.target == Decimal("1000")
    async with clean_tables.session() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(TradeProposal)
            .where(TradeProposal.side == OrderSide.SELL)
        )
    assert count == 0


async def test_rebalance_confirm_proposes_a_partial_trim(clean_tables: Database) -> None:
    await _seed_executed_buy(clean_tables, broker_ticker="AAPL_US_EQ")
    plan = await _rebalancer(clean_tables, risk_max_position_pct="0.01").execute()
    assert "trim proposed" in plan.submitted["AAPL_US_EQ"], plan.submitted
    async with clean_tables.session() as session:
        sell = await session.scalar(
            sa.select(TradeProposal).where(TradeProposal.side == OrderSide.SELL)
        )
    assert sell is not None
    # 800 of 1800 is ~44% of nine shares, rounded down to whole shares.
    assert Decimal("0") < sell.proposed_quantity < Decimal("9")
    assert sell.research_action == ThesisAction.REDUCE.value


async def test_rebalance_needs_conviction_sizing(clean_tables: Database) -> None:
    await _seed_executed_buy(clean_tables, broker_ticker="AAPL_US_EQ")
    service = ph.service_with(clean_tables, ph.settings())
    plan = await RebalanceService(service, service.account_state).plan()
    assert plan.unavailable and "conviction" in plan.unavailable


async def test_rebalance_confirm_proposes_a_top_up_on_the_latest_thesis(
    clean_tables: Database,
) -> None:
    bought = await _seed_executed_buy(clean_tables, broker_ticker="AAPL_US_EQ")
    plan = await _rebalancer(
        clean_tables, risk_max_position_pct="0.03", risk_max_notional_per_trade="5000"
    ).execute()
    assert plan.submitted["AAPL_US_EQ"] == "top-up proposed", plan.submitted
    async with clean_tables.session() as session:
        buy = await session.scalar(
            sa.select(TradeProposal).where(
                TradeProposal.side == OrderSide.BUY,
                TradeProposal.status != ProposalStatus.EXECUTED,
            )
        )
    assert buy is not None and buy.thesis_id == bought.thesis_id
    # Target 3000, 1800 held: the top-up is at most the 1200 gap.
    assert buy.estimated_notional <= Decimal("1200")
