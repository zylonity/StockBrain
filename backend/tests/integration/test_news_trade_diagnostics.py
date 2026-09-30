"""Integration tests for the read-only news-trade diagnostics.

The tests seed the real tables the diagnostic reads -- ``source``, ``event``,
``event_company_impacts``, ``trade_proposals``, ``broker_orders``,
``thesis_outcomes`` and ``thesis_outcome_grades`` -- and assert on the returned
report.  Every fact a metric depends on is controllable, so each test breaks
exactly one assumption and the report must show the break as missing data
rather than as a zero.

The database is a real PostgreSQL, because the guarantees under test (partial
unique indexes, enum columns, JSONB payloads) do not exist anywhere else.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from stockbrain.db.models.companies import (
    BrokerInstrument,
    Company,
    EventCompanyImpact,
)
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    ImpactDirection,
    OrderSide,
    OrderType,
    OutcomeStatus,
    PriceSource,
    ProposalStatus,
    ResearchStatus,
    ResolutionStatus,
    SourceProvider,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.intelligence import news_trade_diagnostics as diagnostics
from stockbrain.intelligence.news_trade_diagnostics import (
    build_news_trade_diagnostics,
    render_news_trade_diagnostics,
    run_news_trade_diagnostics,
)
from stockbrain.market_data.base import Bar

pytestmark = pytest.mark.integration

#: A fixed instant so the sample is reproducible.  A Monday at noon UTC.
T0 = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)

#: Everything the sample observed is at or before this instant.
AS_OF = T0 + dt.timedelta(hours=6)

_D = Decimal

#: The default outcome magnitudes, as named constants so the dataclass defaults
#: are not function calls.
_DEFAULT_ALPHA = _D("0.04")
_DEFAULT_RETURN = _D("0.05")


@dataclass(slots=True)
class _World:
    company_id: uuid.UUID
    instrument_id: uuid.UUID
    event_id: uuid.UUID | None
    proposal_id: uuid.UUID
    order_id: uuid.UUID
    outcome_id: uuid.UUID | None


@dataclass(slots=True)
class _SeedOptions:
    """Optional knobs for :func:`_seed`; grouped to keep the call sites short."""

    source_specs: Sequence[tuple[dt.datetime | None, dt.datetime]] | None = None
    with_event: bool = True
    with_company: bool = True
    with_impact: bool = False
    with_outcome: bool = False
    outcome_status: OutcomeStatus = OutcomeStatus.CLOSED
    closed_at: dt.datetime | None = None
    close_grade_at: dt.datetime | None = None
    window_grade_at: dt.datetime | None = None
    outcome_alpha: Decimal = _DEFAULT_ALPHA
    outcome_return: Decimal = _DEFAULT_RETURN


async def _seed(
    session: Any,
    *,
    suffix: str = "1",
    ticker: str | None = None,
    environment: str = "live",
    side: OrderSide = OrderSide.BUY,
    broker_status: str = "FILLED",
    quantity: Decimal = _D("10"),
    filled_quantity: Decimal | None = _D("10"),
    executed_at: dt.datetime | None = T0,
    submitted_at: dt.datetime | None = T0,
    raw: dict[str, Any] | None = None,
    options: _SeedOptions | None = None,
) -> _World:
    """Seed one live-or-demo BUY world that the diagnostic can read."""
    options = options or _SeedOptions()
    source_specs = (
        options.source_specs
        if options.source_specs is not None
        else ((T0 - dt.timedelta(minutes=30), T0 - dt.timedelta(minutes=25)),)
    )

    async def _stage(*objects: Any) -> None:
        """Add and flush one dependency layer.

        Most of these tables are linked by raw foreign keys rather than ORM
        relationships, so a single flush is free to order them wrongly.  Staging
        the flush is what keeps the seed deterministic.
        """
        session.add_all(list(objects))
        await session.flush()

    company = Company(id=uuid.uuid4(), name=f"Company {suffix}")
    await _stage(company)
    instrument = BrokerInstrument(
        id=uuid.uuid4(),
        company_id=company.id,
        broker=Broker.TRADING212,
        broker_ticker=ticker or f"T{suffix.upper()}_US_EQ",
        market_symbol=(ticker or f"T{suffix.upper()}_US_EQ").split("_")[0],
        currency="USD",
        is_active=True,
    )
    await _stage(instrument)

    event: Event | None = None
    if options.with_event:
        event = Event(
            id=uuid.uuid4(),
            title=f"Event {suffix}",
            title_hash=uuid.uuid4().hex,
            event_type="EARNINGS",
        )
        await _stage(event)
        sources = [
            Source(
                id=uuid.uuid4(),
                provider=SourceProvider.ALPACA,
                headline=f"Headline {suffix}-{index}",
                published_at=published_at,
                received_at=received_at,
                content_hash=uuid.uuid4().hex,
                provider_item_id=None,
            )
            for index, (published_at, received_at) in enumerate(source_specs)
        ]
        await _stage(*sources)
        await _stage(
            *(EventSourceLink(event_id=event.id, source_id=source.id) for source in sources)
        )
        if options.with_impact:
            await _stage(
                EventCompanyImpact(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    company_id=company.id,
                    company_name_hint=company.name,
                    company_key=f"company-{suffix}",
                    broker_instrument_id=instrument.id,
                    direction=ImpactDirection.POSITIVE,
                    relationship_type="direct",
                    impact_path="direct",
                    materiality_score=0.8,
                    confidence=0.9,
                    resolution_status=ResolutionStatus.RESOLVED,
                )
            )

    proposal = TradeProposal(
        id=uuid.uuid4(),
        created_at=T0,
        updated_at=T0,
        broker=Broker.TRADING212,
        broker_ticker=instrument.broker_ticker,
        account_id="default",
        broker_environment=environment,
        side=side,
        order_type=OrderType.MARKET,
        proposed_quantity=quantity,
        reference_price=_D("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=T0 - dt.timedelta(seconds=1),
        quote_age_ms=500,
        estimated_notional=_D("1000"),
        account_currency="USD",
        event_id=event.id if event is not None else None,
        company_id=company.id if options.with_company else None,
        broker_instrument_id=instrument.id,
        executed_at=executed_at,
        # Terminal, so several seeds can share a broker/ticker without tripping
        # the one-active-proposal-per-instrument index.
        status=ProposalStatus.EXECUTED,
        expires_at=T0 + dt.timedelta(days=1),
        research_action=ThesisAction.BUY.value,
        research_confidence=0.7,
    )
    await _stage(proposal)

    order = BrokerOrder(
        id=uuid.uuid4(),
        created_at=T0,
        updated_at=T0,
        broker=Broker.TRADING212,
        broker_order_id=f"order-{suffix}",
        proposal_id=proposal.id,
        broker_ticker=instrument.broker_ticker,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        filled_quantity=filled_quantity,
        filled_value=_D("1000") if filled_quantity is not None else None,
        broker_status=broker_status,
        broker_environment=environment,
        submitted_at=submitted_at,
        is_terminal=broker_status in {"FILLED", "CANCELLED", "REJECTED"},
        raw=raw if raw is not None else {"filledAt": (executed_at or T0).isoformat()},
    )
    await _stage(order)

    outcome_id: uuid.UUID | None = None
    if options.with_outcome:
        outcome_id = uuid.uuid4()
        run = ResearchRun(
            id=uuid.uuid4(),
            company_id=company.id,
            broker_instrument_id=instrument.id,
            event_id=event.id if event is not None else None,
            status=ResearchStatus.SUCCEEDED,
            as_of=T0,
            completed_at=T0,
        )
        thesis = Thesis(
            id=uuid.uuid4(),
            research_run_id=run.id,
            action=ThesisAction.BUY,
            confidence=0.7,
            time_horizon=TimeHorizon.DAYS,
        )
        outcome = ThesisOutcome(
            id=outcome_id,
            proposal_id=proposal.id,
            thesis_id=thesis.id,
            research_run_id=run.id,
            company_id=company.id,
            broker_instrument_id=instrument.id,
            broker=Broker.TRADING212,
            broker_ticker=instrument.broker_ticker,
            event_type="EARNINGS",
            action=ThesisAction.BUY,
            horizon=TimeHorizon.DAYS,
            confidence=_D("0.7"),
            entry_at=executed_at or T0,
            entry_date=(executed_at or T0).date(),
            reference_price=_D("100"),
            currency="USD",
            benchmark_symbol="SPY",
            status=options.outcome_status,
            closed_at=options.closed_at,
        )
        await _stage(run)
        await _stage(thesis)
        await _stage(outcome)

        resolved_close = options.close_grade_at
        if (
            resolved_close is None
            and options.window_grade_at is None
            and options.outcome_status == OutcomeStatus.CLOSED
        ):
            resolved_close = options.closed_at or (T0 + dt.timedelta(days=1))
        grades: list[ThesisOutcomeGrade] = []
        if resolved_close is not None:
            grades.append(
                _grade(
                    outcome,
                    checkpoint="CLOSE",
                    graded_at=resolved_close,
                    instrument_return=options.outcome_return,
                    alpha=options.outcome_alpha,
                )
            )
        if options.window_grade_at is not None:
            grades.append(
                _grade(
                    outcome,
                    checkpoint="D1",
                    graded_at=options.window_grade_at,
                    instrument_return=options.outcome_return,
                    alpha=options.outcome_alpha,
                )
            )
        if grades:
            await _stage(*grades)

    return _World(
        company_id=company.id,
        instrument_id=instrument.id,
        event_id=event.id if event is not None else None,
        proposal_id=proposal.id,
        order_id=order.id,
        outcome_id=outcome_id,
    )


def _grade(
    outcome: ThesisOutcome,
    *,
    checkpoint: str,
    graded_at: dt.datetime,
    instrument_return: Decimal,
    alpha: Decimal,
) -> ThesisOutcomeGrade:
    entry_close = _D("100")
    benchmark_entry = _D("400")
    return ThesisOutcomeGrade(
        id=uuid.uuid4(),
        outcome_id=outcome.id,
        checkpoint=checkpoint,
        trading_days=1,
        entry_close=entry_close,
        current_close=entry_close * (_D("1") + instrument_return),
        benchmark_entry_close=benchmark_entry,
        benchmark_current_close=benchmark_entry * _D("1.01"),
        instrument_return=instrument_return,
        benchmark_return=_D("0.01"),
        alpha=alpha,
        correct=alpha > 0,
        graded_at=graded_at,
    )


class _StubBars:
    """A cached-bar provider that answers from a fixed mapping."""

    def __init__(self, mapping: dict[str, Sequence[Bar] | None]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, dt.datetime, dt.datetime]] = []

    async def __call__(
        self, broker_ticker: str, start: dt.datetime, end: dt.datetime
    ) -> Sequence[Bar] | None:
        self.calls.append((broker_ticker, start, end))
        return self._mapping.get(broker_ticker)


def _bar(ticker: str, timestamp: dt.datetime, close: Decimal) -> Bar:
    return Bar(
        symbol=ticker,
        timestamp=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
    )


# ---------------------------------------------------------------------------
# Sample construction
# ---------------------------------------------------------------------------
async def test_only_live_filled_buy_orders_enter_the_sample(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _seed(session, suffix="live")
        await _seed(session, suffix="demo", environment="demo")
        await _seed(session, suffix="sell", side=OrderSide.SELL)
        await _seed(session, suffix="unfilled", broker_status="NEW", filled_quantity=None)

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.live_buy_filled_orders == 1
    assert report.samples.eligible_orders == 1
    assert report.samples.with_linked_event == 1


async def test_delay_uses_earliest_publication_available_at_fill(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _seed(
            session,
            suffix="pit",
            options=_SeedOptions(
                source_specs=(
                    # Available before the fill, and the earliest: 30 minutes.
                    (T0 - dt.timedelta(minutes=30), T0 - dt.timedelta(minutes=25)),
                    # Published earlier still, but received after the fill: not
                    # available when the trade was placed, so it must not count.
                    (T0 - dt.timedelta(minutes=40), T0 + dt.timedelta(minutes=5)),
                    # Received before the fill, but published after it: also not
                    # knowable to the trade.
                    (T0 + dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=2)),
                ),
            ),
        )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.with_publication_before_fill == 1
    assert report.samples.with_delay == 1
    assert report.delay_buckets["15-60m"] == 1
    assert sum(report.delay_buckets.values()) == 1


async def test_submission_timing_is_labelled_as_proxy_when_fill_time_is_absent(
    clean_tables: Database,
) -> None:
    async with clean_tables.transaction() as session:
        world = await _seed(session, suffix="proxy", raw={})
        order = await session.get(BrokerOrder, world.order_id)
        assert order is not None
        order.last_synced_at = T0

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)
    assert report.delay_basis_counts == {"order_submitted_at_proxy": 1}
    assert report.samples.with_delay == 0
    assert sum(report.delay_buckets.values()) == 0
    assert report.missing["actual_fill_delay_unknown_submission_proxy"] == 1
    assert any("actual fill timestamp" in caveat for caveat in report.caveats)


async def test_naive_broker_fill_time_is_not_treated_as_an_actual_instant(
    clean_tables: Database,
) -> None:
    async with clean_tables.transaction() as session:
        world = await _seed(session, suffix="naive", raw={"filledAt": "2026-09-30T14:00:00"})
        order = await session.get(BrokerOrder, world.order_id)
        assert order is not None
        order.last_synced_at = T0

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)
    assert report.delay_basis_counts == {"order_submitted_at_proxy": 1}
    assert report.samples.with_delay == 0
    assert report.missing["actual_fill_delay_unknown_submission_proxy"] == 1


async def test_observations_after_as_of_are_excluded(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        # Closed by as_of, with a window grade already observable.
        await _seed(
            session,
            suffix="observed",
            options=_SeedOptions(
                with_outcome=True,
                outcome_status=OutcomeStatus.CLOSED,
                closed_at=AS_OF + dt.timedelta(hours=2),
                close_grade_at=AS_OF + dt.timedelta(hours=2),
                window_grade_at=T0 + dt.timedelta(hours=1),
            ),
        )
        # Filled only after as_of: the whole order must be excluded.
        await _seed(
            session,
            suffix="future",
            executed_at=AS_OF + dt.timedelta(hours=1),
            submitted_at=AS_OF + dt.timedelta(hours=1),
        )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.live_buy_filled_orders == 2
    assert report.samples.eligible_orders == 1
    assert report.missing["fill_after_as_of"] == 1
    # The close grade is after as_of, so the outcome is only an observed window.
    assert report.samples.with_closed_outcome == 0
    assert report.samples.with_observed_window_outcome == 1
    assert report.outcomes.closed.samples == 0
    assert report.outcomes.observed_window.samples == 1
    assert report.outcome_by_delay == {}
    assert report.outcome_by_prefill_move == {}


async def test_a_naive_as_of_is_rejected(clean_tables: Database) -> None:
    async with clean_tables.session() as session:
        with pytest.raises(ValueError, match="timezone-aware"):
            await build_news_trade_diagnostics(
                session,
                as_of=dt.datetime(2026, 9, 21, 12, 0),  # noqa: DTZ001
            )


# ---------------------------------------------------------------------------
# Partial fills and missing data
# ---------------------------------------------------------------------------
async def test_partial_fills_are_flagged_not_treated_as_whole(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _seed(
            session,
            suffix="partial",
            broker_status="PARTIALLY_FILLED",
            quantity=_D("10"),
            filled_quantity=_D("4"),
        )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.partial_fills == 1
    assert report.samples.live_buy_filled_orders == 1
    assert any("partial" in caveat.lower() for caveat in report.caveats)


async def test_missing_data_is_counted_never_zeroed(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        # A live, filled BUY with no defensible fill instant at all.
        await _seed(session, suffix="notime", executed_at=None, submitted_at=None, raw={})
        # A fill with no linked event; the outcome should still associate.
        await _seed(
            session,
            suffix="noevent",
            options=_SeedOptions(
                with_event=False,
                with_outcome=True,
                closed_at=T0 + dt.timedelta(hours=1),
            ),
        )
        # A fill with an event but no company (no direct link and no impact).
        await _seed(
            session,
            suffix="nocompany",
            options=_SeedOptions(
                with_company=False,
                with_impact=False,
                with_outcome=True,
                closed_at=T0 + dt.timedelta(hours=1),
            ),
        )
        # A fill whose only source has no publication timestamp.
        await _seed(
            session,
            suffix="nopub",
            options=_SeedOptions(
                source_specs=((None, T0 - dt.timedelta(minutes=5)),),
                with_outcome=True,
                closed_at=T0 + dt.timedelta(hours=1),
            ),
        )
        # A fill with an event and source but no persisted thesis outcome.
        await _seed(session, suffix="nooutcome", options=_SeedOptions(with_outcome=False))

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.missing["missing_fill_timestamp"] == 1
    assert report.missing["no_linked_event"] == 1
    assert report.missing["no_linked_company"] == 1
    assert report.missing["no_publication_available_at_fill"] == 1
    assert report.missing["no_thesis_outcome"] == 1


async def test_company_is_resolved_from_a_persisted_impact(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _seed(
            session,
            suffix="viaimpact",
            options=_SeedOptions(with_company=False, with_impact=True),
        )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.with_linked_company == 1
    assert "no_linked_company" not in report.missing


# ---------------------------------------------------------------------------
# Outcomes and small samples
# ---------------------------------------------------------------------------
async def test_tiny_sample_and_no_causal_caveats_are_explicit(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        await _seed(
            session,
            suffix="one",
            options=_SeedOptions(
                with_outcome=True,
                outcome_status=OutcomeStatus.CLOSED,
                closed_at=T0 + dt.timedelta(hours=1),
                outcome_alpha=_D("0.04"),
                outcome_return=_D("0.05"),
            ),
        )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.with_closed_outcome == 1
    assert report.outcomes.closed.samples == 1
    assert report.outcomes.closed.hit_rate == _D("1")
    assert report.outcome_by_delay["15-60m"].samples == 1
    assert report.outcome_by_delay["15-60m"].mean_alpha == _D("0.04")
    assert any("no causal inference" in caveat.lower() for caveat in report.caveats)
    assert any("tiny sample" in caveat.lower() for caveat in report.caveats)

    rendered = render_news_trade_diagnostics(report)
    assert "Tiny sample" in rendered
    assert "no causal inference" in rendered
    assert report.to_dict()["outcomes"]["closed"]["samples"] == 1


# ---------------------------------------------------------------------------
# Cached bars and pre-fill move
# ---------------------------------------------------------------------------
async def test_prefill_move_uses_only_injected_cached_bars(clean_tables: Database) -> None:
    tickers = {name: f"{name.upper()}_US_EQ" for name in ("up", "down", "empty", "error")}
    bars = _StubBars(
        {
            tickers["up"]: [
                _bar(tickers["up"], T0 - dt.timedelta(hours=1), _D("100")),
                _bar(tickers["up"], T0, _D("103")),
            ],
            tickers["down"]: [
                _bar(tickers["down"], T0 - dt.timedelta(hours=1), _D("100")),
                _bar(tickers["down"], T0, _D("98")),
            ],
            tickers["empty"]: [],
            tickers["error"]: None,
        }
    )
    async with clean_tables.transaction() as session:
        for name, ticker in tickers.items():
            await _seed(
                session,
                suffix=name,
                ticker=ticker,
                options=_SeedOptions(
                    with_outcome=name in {"up", "down"},
                    closed_at=T0 + dt.timedelta(hours=1),
                ),
            )

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF, cached_bars=bars)

    assert report.samples.with_prefill_move == 2
    assert report.prefill_move_buckets[">2%"] == 1
    assert report.prefill_move_buckets["<=-2%"] == 1
    assert report.outcome_by_prefill_move[">2%"].samples == 1
    assert report.outcome_by_prefill_move["<=-2%"].samples == 1
    assert report.missing["prefill_bars_missing"] == 1
    assert report.missing["prefill_cache_unavailable"] == 1


async def test_same_cached_bar_leaves_the_move_unknown(clean_tables: Database) -> None:
    ticker = "SAME_US_EQ"
    bars = _StubBars({ticker: [_bar(ticker, T0 - dt.timedelta(minutes=45), _D("100"))]})
    async with clean_tables.transaction() as session:
        await _seed(session, suffix="same", ticker=ticker)

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF, cached_bars=bars)

    assert report.samples.with_prefill_move == 0
    assert report.missing["prefill_same_cached_bar"] == 1


async def test_prefill_move_is_unknown_when_no_provider_exists(
    clean_tables: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "news_history_bar_provider", lambda _session, **_kwargs: None)
    async with clean_tables.transaction() as session:
        await _seed(session, suffix="noprovider")

    report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)

    assert report.samples.with_prefill_move == 0
    assert report.missing["prefill_bar_provider_unavailable"] == 1
    assert any("cached-bar provider" in caveat for caveat in report.caveats)


async def test_news_history_provider_is_discovered_when_present(
    clean_tables: Database,
) -> None:
    news_history = pytest.importorskip("stockbrain.db.models.news_history")
    ticker = "NHC_US_EQ"
    candle_rows = [
        news_history.NewsHistoryCandle(
            id=uuid.uuid4(),
            symbol="NHC",
            timeframe="1Min",
            provider="stub",
            feed="stub",
            bar_start=T0 - dt.timedelta(hours=1, minutes=1),
            observed_at=T0 - dt.timedelta(hours=1),
            ingested_at=T0,
            open=_D("100"),
            high=_D("100"),
            low=_D("100"),
            close=_D("100"),
            volume=10,
        ),
        news_history.NewsHistoryCandle(
            id=uuid.uuid4(),
            symbol="NHC",
            timeframe="1Min",
            provider="stub",
            feed="stub",
            bar_start=T0 - dt.timedelta(minutes=1),
            observed_at=T0,
            ingested_at=T0,
            open=_D("104"),
            high=_D("104"),
            low=_D("104"),
            close=_D("104"),
            volume=10,
        ),
    ]
    async with clean_tables.transaction() as session:
        await _seed(session, suffix="nhc", ticker=ticker)
        session.add_all(candle_rows)

    try:
        report = await run_news_trade_diagnostics(clean_tables, as_of=AS_OF)
    finally:
        async with clean_tables.transaction() as session:
            for row in candle_rows:
                await session.delete(await session.get(news_history.NewsHistoryCandle, row.id))

    assert report.samples.with_prefill_move == 1
    assert report.prefill_move_buckets[">2%"] == 1
