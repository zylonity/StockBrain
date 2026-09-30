"""Read-only diagnostics for live, news-driven BUY fills.

This module answers one descriptive question for an operator: for the live BUY
orders StockBrain actually filled, how long after a linked story did the fill
happen, had the price already moved by then, and how did the associated thesis
outcomes turn out?  It is a *measurement* surface.  It sends nothing to a
broker, writes nothing, changes no setting, and is not a signal generator.

Three rules shape the code, and each exists because the opposite would produce a
number that looks precise and is not:

* **Live means live, and an order must be a filled BUY.**  The base sample is
  ``broker_environment == "live"`` orders on the BUY side that the broker
  actually filled (fully or partially).  Demo rows, sells and unfilled orders
  never enter it.
* **No observation from after ``as_of`` may leak in.**  ``as_of`` is required
  and timezone-aware.  Sources, grades and cached bars are all filtered to
  ``<= as_of`` in addition to their own window guards, so the report is
  reproducible and cannot quietly see the future.
* **A metric is reported only when the data behind it is defensible.**  The
  publication-to-fill delay uses the earliest source publication that was both
  *published* and *received* at or before the fill; a source that arrived later
  did not inform the trade and must not be credited with it.  The pre-fill move
  comes only from persistent, already-cached bars -- this module never fetches
  market data.  Realised outcomes are associated only where a graded close or a
  graded observation window exists as of ``as_of``.

Everything the report cannot know is counted as missing rather than coerced,
estimated or left to look like zero.  Nothing in the output supports a causal
claim: a delay that is shorter for winners is a description of a handful of
trades, not evidence that acting faster would have helped.
"""

from __future__ import annotations

import datetime as dt
import importlib
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.sources import EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import OrderSide, OutcomeCheckpoint
from stockbrain.market_data.base import Bar

__all__ = [
    "LIVE_BROKER_ENVIRONMENT",
    "NewsTradeDiagnostics",
    "NewsTradeOutcomes",
    "OutcomeSummary",
    "SampleCounts",
    "build_news_trade_diagnostics",
    "news_history_bar_provider",
    "render_news_trade_diagnostics",
    "run_news_trade_diagnostics",
]

#: The only broker environment this diagnostic ever measures.
LIVE_BROKER_ENVIRONMENT = "live"

#: Broker statuses that mean some quantity changed hands.  ``PARTIALLY_FILLED``
#: is included deliberately: a partial fill is still an actual fill, and it is
#: counted separately so it cannot silently stand in for a full one.
FILLED_BROKER_STATUSES = frozenset({"FILLED", "PARTIALLY_FILLED"})

#: Below this many fills, a rate or a mean is a description of noise, and the
#: report says so in its caveats.
TINY_SAMPLE_THRESHOLD = 30

#: The cached-bar model named by the sibling news/candle workstream.  It is
#: discovered dynamically rather than imported at module scope, so this
#: diagnostic keeps working -- with the price metric unknown -- whether or not
#: that model has shipped.  Coordinate through :data:`CachedBarProvider`.
NEWS_HISTORY_MODULE = "stockbrain.db.models.news_history"

#: How far before the earliest publication the cached-bar lookup may reach, so a
#: bar at or before that publication can be found without asking for all history.
PREFILL_QUERY_PAD = dt.timedelta(days=14)

#: Raw broker-payload keys that, when present, are the most direct fill timestamp
#: a Trading 212-style order may carry.  Ordered by preference.
_RAW_FILL_TIMESTAMP_KEYS: tuple[str, ...] = (
    "filledAt",
    "filled_at",
    "executedAt",
    "executed_at",
    "dateExecuted",
    "date_executed",
    "fillTime",
    "fill_time",
)

#: A cached-bar lookup.  The provider owns all I/O; this module only reads what
#: it is handed and never calls a network provider itself.  The signature is
#: positional on purpose so a plain ``async def`` function, a bound method or a
#: small adapter class can all be passed.  ``None`` means the cache was
#: unavailable (missing model or failed read), which is counted as missing data
#: rather than as an absence of bars.
CachedBarProvider = Callable[[str, dt.datetime, dt.datetime], Awaitable[Sequence[Bar] | None]]

#: Half-open ``(label, exclusive upper bound)`` buckets; the last bound is None.
_DELAY_BUCKETS: tuple[tuple[str, dt.timedelta | None], ...] = (
    ("<1m", dt.timedelta(minutes=1)),
    ("1-5m", dt.timedelta(minutes=5)),
    ("5-15m", dt.timedelta(minutes=15)),
    ("15-60m", dt.timedelta(hours=1)),
    ("1-4h", dt.timedelta(hours=4)),
    ("4-24h", dt.timedelta(days=1)),
    ("1-3d", dt.timedelta(days=3)),
    (">3d", None),
)

#: Inclusive upper bounds for the fraction return from the earliest publication
#: to the fill.  The first is ``move <= -2%``; the last is ``move > 2%``.
_PREFILL_MOVE_BUCKETS: tuple[tuple[str, Decimal | None], ...] = (
    ("<=-2%", Decimal("-0.02")),
    ("-2%..-0.5%", Decimal("-0.005")),
    ("-0.5%..0.5%", Decimal("0.005")),
    ("0.5%..2%", Decimal("0.02")),
    (">2%", None),
)

PREFILL_WINDOW = "publication close -> fill or submission-proxy close; see timing basis"

_SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class SampleCounts:
    """How many rows survived each successive, explicitly named filter."""

    live_buy_filled_orders: int = 0
    eligible_orders: int = 0
    partial_fills: int = 0
    with_linked_event: int = 0
    with_linked_company: int = 0
    with_publication_before_fill: int = 0
    with_delay: int = 0
    with_prefill_move: int = 0
    with_closed_outcome: int = 0
    with_observed_window_outcome: int = 0


@dataclass(frozen=True, slots=True)
class OutcomeSummary:
    """Descriptive statistics for one defensible outcome group.

    ``hit_rate`` is the share whose stored ``correct`` flag is true, i.e. the
    share whose alpha had the sign the thesis predicted.  It is a *rate over a
    sample*, never a probability.
    """

    samples: int = 0
    mean_instrument_return: Decimal | None = None
    median_instrument_return: Decimal | None = None
    mean_alpha: Decimal | None = None
    median_alpha: Decimal | None = None
    hit_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NewsTradeOutcomes:
    """Closed outcomes, observed windows, and the two taken together.

    ``closed`` is a stored CLOSE grade, not broker-realised P&L. The grade uses
    daily market closes and a reference entry price. ``observed_window`` is an
    interim checkpoint grade (D1/D5/...). Keeping them apart stops an interim
    number being read as a completed holding period.
    """

    closed: OutcomeSummary = OutcomeSummary()
    observed_window: OutcomeSummary = OutcomeSummary()
    combined: OutcomeSummary = OutcomeSummary()


@dataclass(frozen=True, slots=True)
class NewsTradeDiagnostics:
    """The complete, serialisable result of one read-only diagnostic pass."""

    as_of: dt.datetime
    broker_environment: str
    samples: SampleCounts
    delay_buckets: dict[str, int]
    delay_basis_counts: dict[str, int]
    prefill_move_buckets: dict[str, int]
    prefill_window: str
    outcomes: NewsTradeOutcomes
    outcome_by_delay: dict[str, OutcomeSummary]
    outcome_by_prefill_move: dict[str, OutcomeSummary]
    missing: dict[str, int]
    caveats: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready view; ``Decimal`` values become strings, not floats."""
        return {
            "as_of": self.as_of.isoformat(),
            "broker_environment": self.broker_environment,
            "samples": {
                "live_buy_filled_orders": self.samples.live_buy_filled_orders,
                "eligible_orders": self.samples.eligible_orders,
                "partial_fills": self.samples.partial_fills,
                "with_linked_event": self.samples.with_linked_event,
                "with_linked_company": self.samples.with_linked_company,
                "with_publication_before_fill": self.samples.with_publication_before_fill,
                "with_delay": self.samples.with_delay,
                "with_prefill_move": self.samples.with_prefill_move,
                "with_closed_outcome": self.samples.with_closed_outcome,
                "with_observed_window_outcome": self.samples.with_observed_window_outcome,
            },
            "delay_buckets": dict(self.delay_buckets),
            "delay_basis_counts": dict(self.delay_basis_counts),
            "prefill_move_buckets": dict(self.prefill_move_buckets),
            "prefill_window": self.prefill_window,
            "outcomes": {
                "closed": _summary_dict(self.outcomes.closed),
                "observed_window": _summary_dict(self.outcomes.observed_window),
                "combined": _summary_dict(self.outcomes.combined),
            },
            "outcome_by_delay": {
                key: _summary_dict(value) for key, value in self.outcome_by_delay.items()
            },
            "outcome_by_prefill_move": {
                key: _summary_dict(value) for key, value in self.outcome_by_prefill_move.items()
            },
            "missing": dict(self.missing),
            "caveats": list(self.caveats),
        }


@dataclass(slots=True)
class _OrderFacts:
    """One in-scope order plus what has been resolved about it so far."""

    order: BrokerOrder
    proposal: TradeProposal
    fill_at: dt.datetime
    fill_source: str
    is_partial: bool
    earliest_publication: dt.datetime | None = None
    delay_bucket: str | None = None
    prefill_bucket: str | None = None


async def build_news_trade_diagnostics(
    session: AsyncSession,
    *,
    as_of: dt.datetime,
    broker_environment: str = LIVE_BROKER_ENVIRONMENT,
    cached_bars: CachedBarProvider | None = None,
    tiny_sample_threshold: int = TINY_SAMPLE_THRESHOLD,
) -> NewsTradeDiagnostics:
    """Measure live news-driven BUY fills without ever leaving the database.

    ``as_of`` is mandatory and must be timezone-aware: it is the reproducibility
    boundary, and a diagnostic that defaulted to "now" could silently see data
    the caller did not intend.  ``cached_bars`` is the only source of price
    history; when it is ``None`` the pre-fill move is reported as unknown rather
    than fetched.  The function only reads.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    sample = _MutableSample()
    missing: Counter[str] = Counter()
    delay_counts = _empty_bucket_counts(_DELAY_BUCKETS)
    delay_basis_counts: Counter[str] = Counter()
    prefill_counts = _empty_bucket_counts(_PREFILL_MOVE_BUCKETS)

    rows = (
        await session.execute(
            sa.select(BrokerOrder, TradeProposal)
            .outerjoin(TradeProposal, TradeProposal.id == BrokerOrder.proposal_id)
            .where(
                BrokerOrder.broker_environment == broker_environment,
                BrokerOrder.created_at <= as_of,
                BrokerOrder.side == OrderSide.BUY,
                sa.or_(
                    BrokerOrder.filled_quantity > 0,
                    sa.func.upper(BrokerOrder.broker_status).in_(sorted(FILLED_BROKER_STATUSES)),
                ),
            )
            .order_by(BrokerOrder.created_at, BrokerOrder.broker_order_id)
        )
    ).all()

    facts: list[_OrderFacts] = []
    for order, proposal in rows:
        sample.live_buy_filled_orders += 1
        is_partial = _is_partial_fill(order)
        if is_partial:
            sample.partial_fills += 1
        if proposal is None:
            missing["no_linked_proposal"] += 1
            continue
        if proposal.created_at > as_of:
            missing["proposal_after_as_of"] += 1
            continue
        if proposal.broker_environment != broker_environment:
            missing["proposal_environment_mismatch"] += 1
            continue
        fill_at, fill_source, had_candidates = _resolve_fill_timestamp(order, proposal, as_of)
        if fill_at is None:
            missing["fill_after_as_of" if had_candidates else "missing_fill_timestamp"] += 1
            continue
        sample.eligible_orders += 1
        facts.append(
            _OrderFacts(
                order=order,
                proposal=proposal,
                fill_at=fill_at,
                fill_source=fill_source or "unknown",
                is_partial=is_partial,
            )
        )

    event_ids = sorted({f.proposal.event_id for f in facts if f.proposal.event_id is not None})
    company_by_event = await _load_company_links(session, event_ids)
    sources_by_event = await _load_event_sources(session, event_ids, as_of)

    for f in facts:
        event_id = f.proposal.event_id
        if event_id is None:
            missing["no_linked_event"] += 1
            continue
        sample.with_linked_event += 1
        if f.proposal.company_id is not None or company_by_event.get(event_id) is not None:
            sample.with_linked_company += 1
        else:
            missing["no_linked_company"] += 1

        available = [
            published_at
            for published_at, received_at in sources_by_event.get(event_id, ())
            if received_at <= f.fill_at and published_at <= f.fill_at
        ]
        if not available:
            missing["no_publication_available_at_fill"] += 1
            continue
        earliest = min(available)
        f.earliest_publication = earliest
        sample.with_publication_before_fill += 1
        delay = f.fill_at - earliest
        if delay < dt.timedelta(0):  # unreachable given the guard; kept explicit
            missing["negative_delay"] += 1
            continue
        delay_basis_counts[f.fill_source] += 1
        if f.fill_source == "broker_raw_fill":
            sample.with_delay += 1
            f.delay_bucket = _delay_bucket(delay)
            delay_counts[f.delay_bucket] += 1
        else:
            # Submission is an observable event, but the execution instant may
            # be later. Never put this proxy in an actual-fill delay cohort.
            missing["actual_fill_delay_unknown_submission_proxy"] += 1

    if cached_bars is None:
        instrument_ids = {
            f.proposal.broker_instrument_id
            for f in facts
            if f.proposal.broker_instrument_id is not None
        }
        instrument_rows = (
            (
                await session.scalars(
                    sa.select(BrokerInstrument).where(BrokerInstrument.id.in_(instrument_ids))
                )
            ).all()
            if instrument_ids
            else []
        )
        by_id = {row.id: row.market_symbol for row in instrument_rows}
        market_symbols = {
            f.proposal.broker_ticker: (
                by_id.get(f.proposal.broker_instrument_id)
                if f.proposal.broker_instrument_id is not None
                else None
            )
            for f in facts
        }
        cached_bars = news_history_bar_provider(
            session, symbol_lookup=market_symbols.get, as_of=as_of
        )
    if cached_bars is None:
        missing["prefill_bar_provider_unavailable"] += sum(
            1 for f in facts if f.earliest_publication is not None
        )
    else:
        for f in facts:
            if f.earliest_publication is None:
                continue
            move = await _prefill_move(cached_bars, f, as_of, missing)
            if move is None:
                continue
            sample.with_prefill_move += 1
            f.prefill_bucket = _prefill_bucket(move)
            prefill_counts[f.prefill_bucket] += 1

    outcomes, outcome_missing, by_delay, by_prefill = await _collect_outcomes(
        session, facts, as_of, sample
    )
    missing.update(outcome_missing)

    caveats = _caveats(sample, missing, tiny_sample_threshold)
    return NewsTradeDiagnostics(
        as_of=as_of,
        broker_environment=broker_environment,
        samples=sample.freeze(),
        delay_buckets=delay_counts,
        delay_basis_counts=dict(delay_basis_counts),
        prefill_move_buckets=prefill_counts,
        prefill_window=PREFILL_WINDOW,
        outcomes=outcomes,
        outcome_by_delay=by_delay,
        outcome_by_prefill_move=by_prefill,
        missing=dict(sorted(missing.items())),
        caveats=caveats,
    )


async def run_news_trade_diagnostics(
    database: Database,
    *,
    as_of: dt.datetime,
    broker_environment: str = LIVE_BROKER_ENVIRONMENT,
    cached_bars: CachedBarProvider | None = None,
    tiny_sample_threshold: int = TINY_SAMPLE_THRESHOLD,
) -> NewsTradeDiagnostics:
    """Convenience wrapper that opens a short read-only session of its own."""
    async with database.session() as session:
        return await build_news_trade_diagnostics(
            session,
            as_of=as_of,
            broker_environment=broker_environment,
            cached_bars=cached_bars,
            tiny_sample_threshold=tiny_sample_threshold,
        )


def news_history_bar_provider(
    session: AsyncSession,
    *,
    symbol_lookup: Callable[[str], str | None] | None = None,
    as_of: dt.datetime | None = None,
) -> CachedBarProvider | None:
    """A cached-bar provider over the sibling ``news_history`` model, or ``None``.

    The model is discovered dynamically, so this module neither imports nor
    type-depends on a workstream that may not have shipped: when
    :data:`NEWS_HISTORY_MODULE` is absent, or has no ``NewsHistoryCandle``,
    ``None`` is returned and the pre-fill move stays unknown.

    ``symbol_lookup`` maps a broker ticker to whatever symbol the cache keys
    candles by; the broker ticker is used unchanged when no mapping is supplied.
    A read failure returns ``None`` (counted as an unavailable cache) rather than
    aborting the whole diagnostic.  Bars are timestamped by ``observed_at`` --
    when the close first became knowable -- so the pick is point-in-time correct.
    """
    try:
        module = importlib.import_module(NEWS_HISTORY_MODULE)
    except ImportError:
        return None
    candle = getattr(module, "NewsHistoryCandle", None)
    if candle is None:
        return None

    async def provider(
        broker_ticker: str, start: dt.datetime, end: dt.datetime
    ) -> Sequence[Bar] | None:
        symbol = symbol_lookup(broker_ticker) if symbol_lookup is not None else broker_ticker
        if symbol is None:
            return ()
        try:
            filters = [
                candle.symbol == symbol,
                candle.timeframe == "1Min",
                candle.observed_at >= start,
                candle.observed_at <= end,
            ]
            if as_of is not None:
                filters.append(candle.ingested_at <= as_of)
            feed = (
                await session.execute(
                    sa.select(candle.feed)
                    .where(*filters)
                    .group_by(candle.feed)
                    .order_by(sa.func.count().desc(), candle.feed)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if feed is None:
                return ()
            rows = (
                (
                    await session.execute(
                        sa.select(candle)
                        .where(*filters, candle.feed == feed)
                        .order_by(candle.observed_at)
                    )
                )
                .scalars()
                .all()
            )
        except sa.exc.SQLAlchemyError:
            return None
        return [
            Bar(
                symbol=symbol,
                timestamp=row.observed_at,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                trade_count=row.trade_count,
                vwap=row.vwap,
                feed=row.feed,
            )
            for row in rows
        ]

    return provider


def render_news_trade_diagnostics(report: NewsTradeDiagnostics) -> str:
    """Render the report as a compact, plain-text block (no printing here)."""
    samples = report.samples
    lines = [
        f"News trade diagnostics (as_of={report.as_of.isoformat()}, "
        f"env={report.broker_environment})",
        (
            "Samples: "
            f"live_buy_filled={samples.live_buy_filled_orders} "
            f"eligible={samples.eligible_orders} "
            f"partial={samples.partial_fills} "
            f"event={samples.with_linked_event} "
            f"company={samples.with_linked_company} "
            f"publication={samples.with_publication_before_fill} "
            f"delay={samples.with_delay} "
            f"prefill={samples.with_prefill_move} "
            f"closed={samples.with_closed_outcome} "
            f"window={samples.with_observed_window_outcome}"
        ),
        "Publication->fill/submit timing buckets: " + _render_buckets(report.delay_buckets),
        "Timing basis: " + _render_buckets(report.delay_basis_counts),
        f"Pre-fill move buckets ({report.prefill_window}): "
        + _render_buckets(report.prefill_move_buckets),
        "Outcomes (closed): " + _render_summary(report.outcomes.closed),
        "Outcomes (observed window): " + _render_summary(report.outcomes.observed_window),
        "Outcomes (combined): " + _render_summary(report.outcomes.combined),
        "Outcome by delay: "
        + "; ".join(
            f"{key}: {_render_summary(value)}" for key, value in report.outcome_by_delay.items()
        ),
        "Outcome by pre-fill move: "
        + "; ".join(
            f"{key}: {_render_summary(value)}"
            for key, value in report.outcome_by_prefill_move.items()
        ),
        "Missing data: " + _render_buckets(report.missing),
    ]
    lines.extend(f"Caveat: {caveat}" for caveat in report.caveats)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------
def _is_partial_fill(order: BrokerOrder) -> bool:
    status = (order.broker_status or "").upper()
    if status == "PARTIALLY_FILLED":
        return True
    filled = order.filled_quantity
    if filled is None or order.quantity is None:
        return False
    return filled < order.quantity


def _resolve_fill_timestamp(
    order: BrokerOrder, proposal: TradeProposal, as_of: dt.datetime
) -> tuple[dt.datetime | None, str | None, bool]:
    """The best defensible fill instant at or before ``as_of``.

    An explicit broker fill timestamp is preferred. Trading 212's current
    stored order payload has no fill timestamp, so a submission-time proxy is
    returned with a distinct basis; it is never presented as an actual fill.
    A proxy is usable only when the order status was synced by ``as_of``.
    """
    raw_timestamp = _raw_fill_timestamp(order.raw)
    if raw_timestamp is not None:
        normalised = _as_utc(raw_timestamp)
        if normalised <= as_of:
            return normalised, "broker_raw_fill", True
        return None, None, True
    if order.last_synced_at > as_of:
        return None, None, False
    for candidate, basis in (
        (order.submitted_at, "order_submitted_at_proxy"),
        (proposal.executed_at, "proposal_executed_at_proxy"),
    ):
        if candidate is not None and _as_utc(candidate) <= as_of:
            return _as_utc(candidate), basis, True
    return None, None, False


def _raw_fill_timestamp(raw: object) -> dt.datetime | None:
    if not isinstance(raw, dict):
        return None
    for key in _RAW_FILL_TIMESTAMP_KEYS:
        parsed = _parse_timestamp(raw.get(key))
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    return None


def _parse_timestamp(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


async def _load_event_sources(
    session: AsyncSession, event_ids: Sequence[uuid.UUID], as_of: dt.datetime
) -> dict[uuid.UUID, list[tuple[dt.datetime, dt.datetime]]]:
    """Publications and receipts per event, already bounded by ``as_of``."""
    if not event_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(EventSourceLink.event_id, Source.published_at, Source.received_at)
            .join(Source, Source.id == EventSourceLink.source_id)
            .where(
                EventSourceLink.event_id.in_(list(event_ids)),
                Source.published_at.is_not(None),
                Source.published_at <= as_of,
                Source.received_at <= as_of,
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[tuple[dt.datetime, dt.datetime]]] = {}
    for event_id, published_at, received_at in rows:
        if published_at is None:  # narrowed above; kept for the type checker
            continue
        grouped.setdefault(event_id, []).append((published_at, received_at))
    return grouped


async def _load_company_links(
    session: AsyncSession, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Event -> resolved company, from the classifier's persisted impacts."""
    if not event_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(
                EventCompanyImpact.event_id,
                EventCompanyImpact.company_id,
                EventCompanyImpact.broker_instrument_id,
            ).where(
                EventCompanyImpact.event_id.in_(list(event_ids)),
                EventCompanyImpact.company_id.is_not(None),
            )
        )
    ).all()
    resolved: dict[uuid.UUID, uuid.UUID] = {}
    for event_id, company_id, _instrument_id in rows:
        if company_id is not None and event_id not in resolved:
            resolved[event_id] = company_id
    return resolved


async def _prefill_move(
    cached_bars: CachedBarProvider,
    facts: _OrderFacts,
    as_of: dt.datetime,
    missing: Counter[str],
) -> Decimal | None:
    """The publication-close -> fill-close return, from cached bars only.

    A bar at or before the earliest *available* publication anchors the start
    and a bar at or before the fill anchors the end.  Either anchor missing, or
    both anchors landing on the same bar, leaves the move unknown rather than
    reporting a fabricated zero.
    """
    assert facts.earliest_publication is not None
    bars = await cached_bars(
        facts.proposal.broker_ticker,
        facts.earliest_publication - PREFILL_QUERY_PAD,
        facts.fill_at,
    )
    if bars is None:
        missing["prefill_cache_unavailable"] += 1
        return None
    usable = [bar for bar in bars if _as_utc(bar.timestamp) <= as_of]
    start_bar = _bar_at_or_before(usable, facts.earliest_publication)
    end_bar = _bar_at_or_before(usable, facts.fill_at)
    if start_bar is None or end_bar is None:
        missing["prefill_bars_missing"] += 1
        return None
    if start_bar.timestamp == end_bar.timestamp:
        missing["prefill_same_cached_bar"] += 1
        return None
    if start_bar.close <= 0:
        missing["prefill_nonpositive_close"] += 1
        return None
    return (end_bar.close - start_bar.close) / start_bar.close


def _bar_at_or_before(bars: Sequence[Bar], moment: dt.datetime) -> Bar | None:
    found: Bar | None = None
    for bar in bars:
        if _as_utc(bar.timestamp) > moment:
            break
        found = bar
    return found


async def _collect_outcomes(
    session: AsyncSession,
    facts: Sequence[_OrderFacts],
    as_of: dt.datetime,
    sample: _MutableSample,
) -> tuple[
    NewsTradeOutcomes,
    Counter[str],
    dict[str, OutcomeSummary],
    dict[str, OutcomeSummary],
]:
    """Associate persisted outcomes with the sample, but only up to ``as_of``.

    An outcome is *closed* when it carries a CLOSE grade (and, if it records a
    ``closed_at``, that close is no later than ``as_of``).  It is an *observed
    window* when it has a checkpoint grade but no CLOSE grade.  Anything else is
    missing, not zero.
    """
    missing: Counter[str] = Counter()
    proposal_ids = sorted({f.proposal.id for f in facts})
    if not proposal_ids:
        return NewsTradeOutcomes(), missing, {}, {}

    outcomes = list(
        (
            await session.execute(
                sa.select(ThesisOutcome).where(ThesisOutcome.proposal_id.in_(proposal_ids))
            )
        )
        .scalars()
        .all()
    )
    outcome_by_proposal = {outcome.proposal_id: outcome for outcome in outcomes}
    grades_by_outcome = await _load_grades(session, [o.id for o in outcomes], as_of)

    closed_rows: list[tuple[Decimal, Decimal, bool]] = []
    window_rows: list[tuple[Decimal, Decimal, bool]] = []
    # Association buckets use completed thesis grades only; an interim D1/D5
    # checkpoint has a different horizon and must not be pooled with CLOSE.
    delay_rows: dict[str, list[tuple[Decimal, Decimal, bool]]] = {}
    prefill_rows: dict[str, list[tuple[Decimal, Decimal, bool]]] = {}
    seen: set[uuid.UUID] = set()
    for f in facts:
        outcome = outcome_by_proposal.get(f.proposal.id)
        if outcome is None:
            missing["no_thesis_outcome"] += 1
            continue
        if outcome.id in seen:
            continue
        seen.add(outcome.id)
        close_grade, window_grade = _select_grades(grades_by_outcome.get(outcome.id, ()))
        if close_grade is not None and (
            outcome.closed_at is None or _as_utc(outcome.closed_at) <= as_of
        ):
            measured = _grade_tuple(close_grade)
            closed_rows.append(measured)
            sample.with_closed_outcome += 1
            if f.delay_bucket is not None:
                delay_rows.setdefault(f.delay_bucket, []).append(measured)
            if f.prefill_bucket is not None:
                prefill_rows.setdefault(f.prefill_bucket, []).append(measured)
        elif window_grade is not None:
            measured = _grade_tuple(window_grade)
            window_rows.append(measured)
            sample.with_observed_window_outcome += 1
        else:
            missing["no_outcome_evidence_at_or_before_as_of"] += 1
            continue

    return (
        NewsTradeOutcomes(
            closed=_summarise(closed_rows),
            observed_window=_summarise(window_rows),
            combined=_summarise(closed_rows + window_rows),
        ),
        missing,
        {key: _summarise(rows) for key, rows in sorted(delay_rows.items())},
        {key: _summarise(rows) for key, rows in sorted(prefill_rows.items())},
    )


async def _load_grades(
    session: AsyncSession, outcome_ids: Sequence[uuid.UUID], as_of: dt.datetime
) -> dict[uuid.UUID, list[ThesisOutcomeGrade]]:
    if not outcome_ids:
        return {}
    grades = list(
        (
            await session.execute(
                sa.select(ThesisOutcomeGrade)
                .where(
                    ThesisOutcomeGrade.outcome_id.in_(list(outcome_ids)),
                    ThesisOutcomeGrade.graded_at <= as_of,
                )
                .order_by(ThesisOutcomeGrade.graded_at)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ThesisOutcomeGrade]] = {}
    for grade in grades:
        grouped.setdefault(grade.outcome_id, []).append(grade)
    return grouped


def _select_grades(
    grades: Sequence[ThesisOutcomeGrade],
) -> tuple[ThesisOutcomeGrade | None, ThesisOutcomeGrade | None]:
    close_grade: ThesisOutcomeGrade | None = None
    window_grade: ThesisOutcomeGrade | None = None
    for grade in grades:
        if grade.checkpoint == OutcomeCheckpoint.CLOSE.value:
            if close_grade is None or grade.graded_at > close_grade.graded_at:
                close_grade = grade
        elif window_grade is None or grade.graded_at > window_grade.graded_at:
            window_grade = grade
    return close_grade, window_grade


def _grade_tuple(grade: ThesisOutcomeGrade) -> tuple[Decimal, Decimal, bool]:
    return grade.instrument_return, grade.alpha, grade.correct


# ---------------------------------------------------------------------------
# Aggregation and rendering helpers
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _MutableSample:
    live_buy_filled_orders: int = 0
    eligible_orders: int = 0
    partial_fills: int = 0
    with_linked_event: int = 0
    with_linked_company: int = 0
    with_publication_before_fill: int = 0
    with_delay: int = 0
    with_prefill_move: int = 0
    with_closed_outcome: int = 0
    with_observed_window_outcome: int = 0

    def freeze(self) -> SampleCounts:
        return SampleCounts(
            live_buy_filled_orders=self.live_buy_filled_orders,
            eligible_orders=self.eligible_orders,
            partial_fills=self.partial_fills,
            with_linked_event=self.with_linked_event,
            with_linked_company=self.with_linked_company,
            with_publication_before_fill=self.with_publication_before_fill,
            with_delay=self.with_delay,
            with_prefill_move=self.with_prefill_move,
            with_closed_outcome=self.with_closed_outcome,
            with_observed_window_outcome=self.with_observed_window_outcome,
        )


def _empty_bucket_counts(buckets: Sequence[tuple[str, object]]) -> dict[str, int]:
    counts = {label: 0 for label, _ in buckets}
    counts["unknown"] = 0
    return counts


def _delay_bucket(delay: dt.timedelta) -> str:
    for label, upper in _DELAY_BUCKETS:
        if upper is None or delay < upper:
            return label
    return ">3d"  # pragma: no cover - the None bound always matches


def _prefill_bucket(move: Decimal) -> str:
    for label, upper in _PREFILL_MOVE_BUCKETS:
        if upper is None or move <= upper:
            return label
    return ">2%"  # pragma: no cover - the None bound always matches


def _summarise(rows: Sequence[tuple[Decimal, Decimal, bool]]) -> OutcomeSummary:
    if not rows:
        return OutcomeSummary()
    instrument = [row[0] for row in rows]
    alphas = [row[1] for row in rows]
    correct = sum(1 for row in rows if row[2])
    return OutcomeSummary(
        samples=len(rows),
        mean_instrument_return=_mean(instrument),
        median_instrument_return=_median(instrument),
        mean_alpha=_mean(alphas),
        median_alpha=_median(alphas),
        hit_rate=(Decimal(correct) / Decimal(len(rows))).quantize(_SIX_PLACES, ROUND_HALF_EVEN),
    )


def _mean(values: Sequence[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(_SIX_PLACES, ROUND_HALF_EVEN)


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle].quantize(_SIX_PLACES, ROUND_HALF_EVEN)
    return ((ordered[middle - 1] + ordered[middle]) / Decimal(2)).quantize(
        _SIX_PLACES, ROUND_HALF_EVEN
    )


def _summary_dict(summary: OutcomeSummary) -> dict[str, Any]:
    return {
        "samples": summary.samples,
        "mean_instrument_return": _decimal_str(summary.mean_instrument_return),
        "median_instrument_return": _decimal_str(summary.median_instrument_return),
        "mean_alpha": _decimal_str(summary.mean_alpha),
        "median_alpha": _decimal_str(summary.median_alpha),
        "hit_rate": _decimal_str(summary.hit_rate),
    }


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _render_buckets(counts: dict[str, int]) -> str:
    return " ".join(f"{label}={count}" for label, count in counts.items() if count)


def _render_summary(summary: OutcomeSummary) -> str:
    if summary.samples == 0:
        return "n=0"
    return (
        f"n={summary.samples} mean_alpha={summary.mean_alpha} "
        f"median_alpha={summary.median_alpha} "
        f"mean_return={summary.mean_instrument_return} hit_rate={summary.hit_rate}"
    )


def _caveats(
    sample: _MutableSample, missing: Counter[str], tiny_sample_threshold: int
) -> tuple[str, ...]:
    caveats = [
        "Descriptive only: no causal inference is drawn between publication timing, "
        "price moves or outcomes.",
        "Outcome grades use daily market closes and a reference entry price; "
        "they are not actual fill-to-sale P&L and exclude fees and FX costs.",
        "Timing and pre-entry move association buckets contain completed CLOSE "
        "grades only; interim observed windows are reported separately.",
        "Trading 212 does not persist an actual fill timestamp here. Where the timing "
        "basis is a submission proxy, publication-to-fill delay and the move up to "
        "the actual fill are unknown.",
        "This report is read-only and changes no trading setting, order or rule.",
    ]
    if sample.live_buy_filled_orders < tiny_sample_threshold:
        caveats.append(
            f"Tiny sample: {sample.live_buy_filled_orders} live BUY fill(s) is below the "
            f"{tiny_sample_threshold}-fill threshold; every rate and mean here is noise."
        )
    if missing.get("no_publication_available_at_fill"):
        caveats.append(
            f"Publication timing is unavailable for {missing['no_publication_available_at_fill']} "
            "fill(s); delay buckets describe only the observable subset."
        )
    if missing.get("prefill_bar_provider_unavailable"):
        caveats.append(
            "Pre-fill move is unknown where no cached-bar provider was available; this "
            "diagnostic never fetches bars."
        )
    if missing.get("prefill_cache_unavailable"):
        caveats.append(
            "Pre-fill move is unknown where the cached-bar read failed; the cache is best-effort "
            "and this diagnostic never fetches bars."
        )
    if sample.partial_fills:
        caveats.append(
            f"{sample.partial_fills} partial fill(s) are included; their fill instant is "
            "ambiguous and they are not equivalent to full fills."
        )
    if not sample.with_closed_outcome and not sample.with_observed_window_outcome:
        caveats.append("No outcome evidence exists at or before as_of for this sample.")
    return tuple(caveats)
