"""Persist BUY catalyst evidence and request one deadline position review.

No order or proposal is created here. The review is ordinary research with the
fixed entry-time expectation in its packet. Its verdict stays advisory unless
``NEWS_CATALYST_EXIT_PROPOSALS_ENABLED`` is set, which the operator should do
only after the offline deadline-exit-versus-hold comparison supports it; even
then a SELL or REDUCE goes through the normal proposal, risk, authorization and
execution checks.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import Company
from stockbrain.db.models.news_catalyst import NewsCatalystDeadline
from stockbrain.db.models.news_history import NewsHistoryCandle
from stockbrain.db.models.portfolio import BrokerOrder, Position
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.session import Database
from stockbrain.enums import OrderSide, ThesisAction
from stockbrain.intelligence.news_history import BackfillLimits, BarsProvider, NewsHistoryCache
from stockbrain.intelligence.research import ResearchValidationError
from stockbrain.intelligence.research_service import (
    CATALYST_REVIEW_TRIGGER,
    NEWS_CACHE_REASSESSMENT_TRIGGER,
    ResearchService,
)
from stockbrain.logging import get_logger
from stockbrain.market_data.news_outcomes import REGULAR_SESSION_CLOSE, REGULAR_SESSION_OPEN

NY = ZoneInfo("America/New_York")
FILL_KEYS = ("filledAt", "filled_at", "fillTimestamp", "fill_timestamp", "executionTime")
MAX_DUE_SCAN_DAYS = 30
BENCHMARK = "SPY"
log = get_logger(__name__)


def _actual_fill_at(raw: object) -> dt.datetime | None:
    if not isinstance(raw, dict):
        return None
    for key in FILL_KEYS:
        value = raw.get(key)
        if not isinstance(value, str):
            continue
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            return moment.astimezone(dt.UTC)
    return None


def _packet_evidence(packet: object) -> dict[str, Any]:
    if not isinstance(packet, dict):
        return {}
    context = packet.get("market_context")
    if not isinstance(context, list):
        return {}
    for datum in context:
        if not isinstance(datum, dict) or datum.get("provider") != "news_outcomes":
            continue
        value = datum.get("text")
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            evidence = parsed.get("catalyst_deadline_evidence")
            if isinstance(evidence, dict):
                return evidence
    return {}


def _nonnegative_int(value: object, default: int = 0) -> int:
    if not isinstance(value, (str, int, float, Decimal)):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _completed_session_dates(bars: list[NewsHistoryCandle], now: dt.datetime) -> set[dt.date]:
    """Dates whose regular session has closed: a regular close bar, or a past day.

    A past day counts even without a 16:00 bar, so an early-close session is
    not silently skipped (which would extend the deadline).
    """
    today = now.astimezone(NY).date()
    latest: dict[dt.date, dt.datetime] = {}
    for bar in bars:
        local = bar.bar_start.astimezone(NY)
        date = local.date()
        finish = (bar.bar_start + dt.timedelta(minutes=1)).astimezone(NY)
        if date not in latest or finish > latest[date]:
            latest[date] = finish
    return {
        date for date, finish in latest.items() if date < today or finish.time() >= dt.time(16, 0)
    }


class CatalystMonitor:
    """A bounded scheduler tick; all persistence and dedupe live in PostgreSQL."""

    def __init__(
        self,
        database: Database,
        research: ResearchService,
        *,
        bars: BarsProvider | None = None,
    ) -> None:
        self.database = database
        self.research = research
        self.bars = bars

    async def tick(self, *, as_of: dt.datetime | None = None) -> None:
        now = as_of or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        await self._record_fills(now)
        await self._refresh_candles(now)
        await self._request_due_reviews(now)

    async def _refresh_candles(self, now: dt.datetime) -> None:
        """Top up post-entry minute bars for positions whose deadline could be due.

        Each fetch starts at the newest cached bar, so repeated ticks cost a
        request or two. A failure is logged and the review simply waits.
        """
        if self.bars is None:
            return
        async with self.database.session() as session:
            pending = (
                await session.scalars(
                    sa.select(NewsCatalystDeadline).where(
                        NewsCatalystDeadline.reassessment_requested_at.is_(None),
                        NewsCatalystDeadline.entry_at.is_not(None),
                        NewsCatalystDeadline.deadline_sessions_after_entry.is_not(None),
                    )
                )
            ).all()
            due = [
                item
                for item in pending
                if item.entry_at is not None
                and item.deadline_sessions_after_entry is not None
                and now - item.entry_at >= dt.timedelta(days=item.deadline_sessions_after_entry)
                and now - item.entry_at <= dt.timedelta(days=MAX_DUE_SCAN_DAYS)
            ]
            windows: dict[str, dt.datetime] = {}
            for item in due:
                assert item.entry_at is not None
                for symbol in (item.market_symbol, BENCHMARK):
                    start = item.entry_at - dt.timedelta(minutes=10)
                    windows[symbol] = min(windows.get(symbol, start), start)
            for symbol, start in list(windows.items()):
                latest = await session.scalar(
                    sa.select(sa.func.max(NewsHistoryCandle.bar_start)).where(
                        NewsHistoryCandle.symbol == symbol,
                        NewsHistoryCandle.timeframe == "1Min",
                    )
                )
                if latest is not None and latest > start:
                    windows[symbol] = latest
        cache = NewsHistoryCache(self.database)
        limits = BackfillLimits(
            max_days=MAX_DUE_SCAN_DAYS + 1,
            max_bar_pages=MAX_DUE_SCAN_DAYS + 2,
            max_requests=MAX_DUE_SCAN_DAYS + 2,
        )
        for symbol, start in windows.items():
            if now - start < dt.timedelta(minutes=5):
                continue
            try:
                await cache.backfill_candles(
                    symbol=symbol, start=start, end=now, limits=limits, provider=self.bars
                )
            except Exception as exc:
                log.warning(
                    "catalyst_candle_refresh_failed",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                )

    async def _record_fills(self, now: dt.datetime) -> None:
        async with self.database.transaction() as session:
            rows = (
                await session.execute(
                    sa.select(BrokerOrder, TradeProposal, ResearchRun, Company.primary_symbol)
                    .join(TradeProposal, BrokerOrder.proposal_id == TradeProposal.id)
                    .join(Company, Company.id == TradeProposal.company_id)
                    .join(ResearchRun, TradeProposal.research_run_id == ResearchRun.id)
                    .join(Thesis, TradeProposal.thesis_id == Thesis.id)
                    .where(
                        BrokerOrder.side == OrderSide.BUY,
                        TradeProposal.side == OrderSide.BUY,
                        Thesis.action == ThesisAction.BUY,
                        TradeProposal.event_id.is_not(None),
                        TradeProposal.company_id.is_not(None),
                        BrokerOrder.filled_quantity > 0,
                        sa.func.upper(BrokerOrder.broker_status).in_(
                            ("FILLED", "PARTIALLY_FILLED")
                        ),
                        BrokerOrder.last_synced_at <= now,
                        # A BUY from a pre-fill news-cache rerun is news-driven too.
                        ResearchRun.trigger.in_(("EVENT", NEWS_CACHE_REASSESSMENT_TRIGGER)),
                        Company.primary_symbol.is_not(None),
                        ~sa.exists().where(NewsCatalystDeadline.broker_order_id == BrokerOrder.id),
                    )
                    .order_by(BrokerOrder.created_at.desc())
                    .limit(100)
                )
            ).all()
            for order, proposal, run, market_symbol in rows:
                assert proposal.event_id is not None and proposal.company_id is not None
                evidence = _packet_evidence(run.research_packet)
                deadline = evidence.get("deadline_sessions_after_entry")
                deadline_sessions = _nonnegative_int(deadline) if deadline is not None else None
                actual_fill = _actual_fill_at(order.raw)
                if actual_fill is not None and actual_fill > now:
                    actual_fill = None
                # Trading 212 payloads carry no fill time. A market order's
                # submission is a proxy good to the session, which is the unit
                # the deadline counts in; the basis column says which was used.
                entry = actual_fill
                basis = "broker_raw_fill"
                if entry is None and order.submitted_at is not None and order.submitted_at <= now:
                    entry, basis = order.submitted_at, "order_submitted_at_proxy"
                threshold = _decimal(evidence.get("threshold"))
                await session.execute(
                    insert(NewsCatalystDeadline)
                    .values(
                        id=uuid.uuid4(),
                        broker_order_id=order.id,
                        proposal_id=proposal.id,
                        event_id=proposal.event_id,
                        company_id=proposal.company_id,
                        broker_ticker=order.broker_ticker,
                        market_symbol=str(market_symbol).upper(),
                        entry_at=entry,
                        entry_time_basis=basis if entry else "unknown",
                        expected_threshold=threshold,
                        supporting_sample_size=_nonnegative_int(evidence.get("sample_size")),
                        response_count=_nonnegative_int(evidence.get("responders")),
                        nonresponse_count=_nonnegative_int(evidence.get("nonresponses")),
                        deadline_sessions_after_entry=deadline_sessions if entry else None,
                        buffer_sessions=_nonnegative_int(evidence.get("buffer_sessions"), 1),
                        measurement_status=(
                            "pending"
                            if entry and deadline_sessions is not None
                            else "fill_time_unknown"
                            if entry is None
                            else "evidence_insufficient"
                        ),
                        evidence_snapshot=evidence,
                    )
                    .on_conflict_do_nothing(index_elements=[NewsCatalystDeadline.broker_order_id])
                )

    async def _request_due_reviews(self, now: dt.datetime) -> None:
        async with self.database.transaction() as session:
            pending = (
                await session.scalars(
                    sa.select(NewsCatalystDeadline)
                    .where(
                        NewsCatalystDeadline.reassessment_requested_at.is_(None),
                        NewsCatalystDeadline.entry_at.is_not(None),
                        NewsCatalystDeadline.deadline_sessions_after_entry.is_not(None),
                    )
                    .order_by(NewsCatalystDeadline.created_at)
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for item in pending:
                assert item.entry_at is not None
                assert item.deadline_sessions_after_entry is not None
                if now - item.entry_at > dt.timedelta(days=MAX_DUE_SCAN_DAYS):
                    item.measurement_status = "deadline_data_stale"
                    continue
                stock, market = await self._bars(session, item.market_symbol, item.entry_at, now)
                stock_dates = _completed_session_dates(stock, now)
                market_dates = _completed_session_dates(market, now)
                entry_date = item.entry_at.astimezone(NY).date()
                completed = sorted(date for date in stock_dates & market_dates if date > entry_date)
                if len(completed) < item.deadline_sessions_after_entry:
                    continue
                target_date = (
                    completed[item.deadline_sessions_after_entry - 1]
                    if item.deadline_sessions_after_entry > 0
                    else entry_date
                )
                move = self._relative_move(stock, market, item.entry_at, target_date)
                origin = await session.get(TradeProposal, item.proposal_id)
                run = await session.get(ResearchRun, origin.research_run_id) if origin else None
                held = await session.scalar(
                    sa.select(Position.id)
                    .where(
                        Position.broker_ticker == item.broker_ticker,
                        Position.quantity > 0,
                        Position.company_id == item.company_id,
                    )
                    .limit(1)
                )
                if held is None or origin is None or origin.thesis_id is None or run is None:
                    item.measurement_status = "position_closed_or_origin_missing"
                    item.reassessment_requested_at = now
                    continue
                if run.impact_id is None:
                    item.measurement_status = "impact_missing"
                    item.reassessment_requested_at = now
                    continue
                rerun_id = uuid.uuid5(uuid.NAMESPACE_URL, f"catalyst-deadline-v1:{item.id}")
                # Persist the measurement first: the review's packet reads it.
                item.reassessment_requested_at = now
                item.observed_market_relative_move = move
                try:
                    review_id = await self.research.request(
                        session,
                        run.impact_id,
                        rerun_id=rerun_id,
                        as_of=now,
                        previous_thesis_id=origin.thesis_id,
                    )
                except ResearchValidationError:
                    item.measurement_status = "research_packet_unavailable"
                    continue
                review = await session.get(ResearchRun, review_id)
                if review is not None:
                    review.trigger = CATALYST_REVIEW_TRIGGER
                item.reassessment_run_id = review_id
                item.measurement_status = "bar_proxy" if move is not None else "price_unknown"
                item.review_reason = (
                    "expected_reaction_failed"
                    if move is not None
                    and item.expected_threshold is not None
                    and move < item.expected_threshold
                    else "expected_reaction_observed_or_unknown"
                )

    async def _bars(
        self, session: Any, symbol: str, entry: dt.datetime, now: dt.datetime
    ) -> tuple[list[NewsHistoryCandle], list[NewsHistoryCandle]]:
        end = min(now, entry + dt.timedelta(days=MAX_DUE_SCAN_DAYS))
        result: list[list[NewsHistoryCandle]] = []
        for ticker in (symbol, BENCHMARK):
            rows = (
                await session.scalars(
                    sa.select(NewsHistoryCandle)
                    .where(
                        NewsHistoryCandle.symbol == ticker,
                        NewsHistoryCandle.timeframe == "1Min",
                        NewsHistoryCandle.bar_start >= entry - dt.timedelta(minutes=5),
                        NewsHistoryCandle.bar_start <= end,
                        NewsHistoryCandle.ingested_at <= now,
                    )
                    .order_by(NewsHistoryCandle.bar_start)
                    .limit(15_000)
                )
            ).all()
            # Regular session only: the close is 16:00, not a thin 19:59 print.
            result.append(
                [
                    row
                    for row in rows
                    if REGULAR_SESSION_OPEN
                    <= row.bar_start.astimezone(NY).time()
                    < REGULAR_SESSION_CLOSE
                ]
            )
        return result[0], result[1]

    @staticmethod
    def _relative_move(
        stock: list[NewsHistoryCandle],
        market: list[NewsHistoryCandle],
        entry: dt.datetime,
        target_date: dt.date,
    ) -> Decimal | None:
        stock_entry = next((bar for bar in stock if bar.bar_start >= entry), None)
        market_entry = next((bar for bar in market if bar.bar_start >= entry), None)
        stock_end = next(
            (bar for bar in reversed(stock) if bar.bar_start.astimezone(NY).date() == target_date),
            None,
        )
        market_end = next(
            (bar for bar in reversed(market) if bar.bar_start.astimezone(NY).date() == target_date),
            None,
        )
        if None in (stock_entry, market_entry, stock_end, market_end):
            return None
        assert stock_entry is not None and market_entry is not None
        assert stock_end is not None and market_end is not None
        if stock_entry.open <= 0 or market_entry.open <= 0:
            return None
        return stock_end.close / stock_entry.open - market_end.close / market_entry.open
