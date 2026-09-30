"""Point-in-time, read-only historical news outcomes for research packets."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.news_catalyst import NewsCatalystDeadline
from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    NewsHistoryArticle,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.intelligence.news_outcome_validation import (
    SESSION_ZONE,
    DeadlineEvidence,
    HistoricalEvent,
    assess_failed_reaction,
    derive_catalyst_deadline,
)
from stockbrain.intelligence.research import ResearchDatum, ResearchPacket
from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import (
    ConfoundingPolicy,
    NewsOutcomeInputs,
    compute_news_outcome,
    regular_session_bars,
)

MAX_CANDIDATES = 24
MAX_EXAMPLES = 8
MIN_SUMMARY_SAMPLES = 10
BENCHMARK_SYMBOL = "SPY"
MAX_BARS_PER_SERIES = 20_000
#: Cached candidates must be at least this old, so 1d/5d outcomes can exist;
#: otherwise a busy name's 24 most recent stories are all still unresolved.
COMPLETED_WINDOW_LOOKBACK = dt.timedelta(days=8)


def _bar(row: NewsHistoryCandle) -> Bar:
    return Bar(
        symbol=row.symbol,
        timestamp=row.bar_start,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        trade_count=row.trade_count,
        vwap=row.vwap,
        feed=row.feed,
    )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


class NewsOutcomeResearchProvider:
    """Read a bounded cached cohort; never fetch data during a research run."""

    def __init__(
        self,
        database: Database,
        *,
        benchmark_symbol: str = BENCHMARK_SYMBOL,
        deadline_buffer_sessions: int = 1,
    ) -> None:
        self.database = database
        self.benchmark_symbol = benchmark_symbol.upper()
        self.deadline_buffer_sessions = deadline_buffer_sessions

    async def _coverage(self, session: Any, symbol: str, as_of: dt.datetime) -> dict[str, Any]:
        row = await session.scalar(
            sa.select(NewsHistoryCoverage).where(
                NewsHistoryCoverage.dataset == DATASET_CANDLES,
                NewsHistoryCoverage.scope_key == symbol,
            )
        )
        if row is None or row.last_attempt_at is None or row.last_attempt_at > as_of:
            return {"status": "unknown_at_as_of"}
        return {
            "status": "partial" if row.is_partial else "recorded",
            "covered_start": row.covered_start.isoformat() if row.covered_start else None,
            "covered_end": row.covered_end.isoformat() if row.covered_end else None,
            "detail": row.detail,
        }

    async def _bars(
        self,
        session: Any,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        as_of: dt.datetime,
    ) -> list[Bar]:
        conditions = (
            NewsHistoryCandle.symbol == symbol,
            NewsHistoryCandle.timeframe == "1Min",
            NewsHistoryCandle.bar_start >= start,
            NewsHistoryCandle.bar_start <= end,
            NewsHistoryCandle.observed_at <= as_of,
            NewsHistoryCandle.ingested_at <= as_of,
        )
        feed = (
            await session.execute(
                sa.select(NewsHistoryCandle.feed)
                .where(*conditions)
                .group_by(NewsHistoryCandle.feed)
                .order_by(sa.func.count().desc(), NewsHistoryCandle.feed)
                .limit(1)
            )
        ).scalar_one_or_none()
        if feed is None:
            return []
        rows = (
            await session.scalars(
                sa.select(NewsHistoryCandle)
                .where(*conditions, NewsHistoryCandle.feed == feed)
                .order_by(NewsHistoryCandle.bar_start)
                .limit(MAX_BARS_PER_SERIES + 1)
            )
        ).all()
        if len(rows) > MAX_BARS_PER_SERIES:
            raise ValueError("candle_window_truncated")
        return regular_session_bars([_bar(row) for row in rows])

    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
        data = await self._cohort_context(packet)
        review = await self._deadline_review(packet)
        if review is None:
            return data
        return tuple(
            datum.model_copy(
                update={"text": json.dumps({**json.loads(datum.text), "catalyst_review": review})}
            )
            for datum in data
        )

    async def _deadline_review(self, packet: ResearchPacket) -> dict[str, Any] | None:
        """The fixed entry-time expectation this reassessment is checking, if any.

        Only a review the catalyst monitor requested by ``as_of`` is included;
        the deadline and threshold are the ones recorded at entry, never
        recomputed from later history.
        """
        if packet.previous_thesis_id is None:
            return None
        async with self.database.session() as session:
            item = await session.scalar(
                sa.select(NewsCatalystDeadline)
                .join(TradeProposal, TradeProposal.id == NewsCatalystDeadline.proposal_id)
                .where(
                    TradeProposal.thesis_id == packet.previous_thesis_id,
                    NewsCatalystDeadline.reassessment_requested_at.is_not(None),
                    NewsCatalystDeadline.reassessment_requested_at <= packet.as_of,
                )
                .order_by(NewsCatalystDeadline.reassessment_requested_at.desc())
                .limit(1)
            )
        if item is None or item.deadline_sessions_after_entry is None:
            return None
        evidence = DeadlineEvidence(
            threshold=item.expected_threshold or Decimal("0"),
            sample_size=item.supporting_sample_size,
            responders=item.response_count,
            nonresponses=item.nonresponse_count,
            unknown=0,
            response_quantile_sessions=None,
            buffer_sessions=item.buffer_sessions,
            deadline_sessions_after_entry=item.deadline_sessions_after_entry,
            reason=None,
        )
        # The research itself decides whether new evidence sustains the thesis;
        # the recommendation states what the price reaction alone implies.
        assessment = assess_failed_reaction(
            evidence,
            elapsed_trading_sessions=item.deadline_sessions_after_entry,
            observed_market_relative_move=item.observed_market_relative_move,
            new_evidence_sustains_thesis=False,
        )
        return {
            "entry_at": item.entry_at.isoformat() if item.entry_at else None,
            "entry_time_basis": item.entry_time_basis,
            "expected_market_relative_move": str(item.expected_threshold),
            "deadline_sessions_after_entry": item.deadline_sessions_after_entry,
            "buffer_sessions": item.buffer_sessions,
            "supporting_sample_size": item.supporting_sample_size,
            "observed_market_relative_move_since_entry": str(item.observed_market_relative_move)
            if item.observed_market_relative_move is not None
            else None,
            "measurement": item.measurement_status,
            "outcome": assessment.reason,
            "guidance": (
                "The expected post-entry reaction did not occur by the deadline fixed at "
                "entry. Unless current evidence newly sustains the thesis, favour SELL or "
                "REDUCE over repeating the original BUY."
                if assessment.recommendation == "FAVOUR_SELL_OR_REDUCE_REASSESSMENT"
                else "Reassess the holding on current evidence; the deadline is not extended."
            ),
        }

    async def _cohort_context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
        async with self.database.session() as session:
            coverage = {
                packet.company.symbol: await self._coverage(
                    session, packet.company.symbol, packet.as_of
                ),
                self.benchmark_symbol: await self._coverage(
                    session, self.benchmark_symbol, packet.as_of
                ),
            }
            current = await session.get(Event, packet.event_id)
            event_type = current.event_type if current else None
            if not event_type:
                return (
                    self._datum(packet, {"status": "unavailable", "reason": "event_type_unknown"}),
                )

            # Backfill grouping nodes are ARCHIVED and deliberately have no live
            # impacts or source links. Read the cache itself and count each event
            # once, even when several provider articles describe it.
            historical = (
                await session.scalars(
                    sa.select(NewsHistoryArticle)
                    .where(
                        NewsHistoryArticle.company_id == packet.company.company_id,
                        NewsHistoryArticle.event_id.is_not(None),
                        NewsHistoryArticle.event_id != packet.event_id,
                        NewsHistoryArticle.published_at.is_not(None),
                        # Old enough for the five-session window to have closed.
                        NewsHistoryArticle.published_at
                        < packet.event_time - COMPLETED_WINDOW_LOOKBACK,
                        NewsHistoryArticle.observed_at <= packet.as_of,
                        NewsHistoryArticle.ingested_at <= packet.as_of,
                        NewsHistoryArticle.event_type == event_type,
                    )
                    .order_by(NewsHistoryArticle.published_at.desc())
                    .limit(MAX_CANDIDATES * 4)
                )
            ).all()
            historical_match = "same_company_and_event_type"
            if not historical:
                historical = (
                    await session.scalars(
                        sa.select(NewsHistoryArticle)
                        .where(
                            NewsHistoryArticle.company_id == packet.company.company_id,
                            NewsHistoryArticle.event_id.is_not(None),
                            NewsHistoryArticle.event_id != packet.event_id,
                            NewsHistoryArticle.published_at.is_not(None),
                            # Old enough for the five-session window to have closed.
                            NewsHistoryArticle.published_at
                            < packet.event_time - COMPLETED_WINDOW_LOOKBACK,
                            NewsHistoryArticle.observed_at <= packet.as_of,
                            NewsHistoryArticle.ingested_at <= packet.as_of,
                        )
                        .order_by(NewsHistoryArticle.published_at.desc())
                        .limit(MAX_CANDIDATES * 4)
                    )
                ).all()
                historical_match = "same_company_event_type_unverified_loose"
            grouped: dict[uuid.UUID, list[NewsHistoryArticle]] = {}
            for article in historical:
                if article.event_id is not None:
                    grouped.setdefault(article.event_id, []).append(article)
            cached_candidates = list(grouped.items())[:MAX_CANDIDATES]

            # Keep the existing live-event path for genuinely classified prior
            # stories; those have an observed receipt timestamp.
            query = (
                sa.select(Event, EventCompanyImpact)
                .join(EventCompanyImpact, EventCompanyImpact.event_id == Event.id)
                .where(
                    EventCompanyImpact.company_id == packet.company.company_id,
                    EventCompanyImpact.broker_instrument_id == packet.company.broker_instrument_id,
                    Event.id != packet.event_id,
                    Event.first_seen_at <= packet.as_of,
                    sa.func.coalesce(Event.classified_at, Event.first_seen_at) <= packet.as_of,
                    EventCompanyImpact.created_at <= packet.as_of,
                    Event.event_time < packet.event_time,
                    Event.merged_into_event_id.is_(None),
                    Event.event_type == event_type,
                    sa.exists(
                        sa.select(Source.id)
                        .join(EventSourceLink, EventSourceLink.source_id == Source.id)
                        .where(
                            EventSourceLink.event_id == Event.id,
                            EventSourceLink.created_at <= packet.as_of,
                            Source.published_at.is_not(None),
                            Source.published_at <= packet.as_of,
                            Source.received_at <= packet.as_of,
                        )
                    ),
                )
                .order_by(Event.event_time.desc(), Event.id)
                .limit(MAX_CANDIDATES * 2)
            )
            candidates = (await session.execute(query)).all()
            match = "same_company_and_event_type"
            if not candidates:
                # Construct a fresh statement: the fallback is visibly looser.
                candidates = (
                    await session.execute(
                        sa.select(Event, EventCompanyImpact)
                        .join(EventCompanyImpact, EventCompanyImpact.event_id == Event.id)
                        .where(
                            EventCompanyImpact.company_id == packet.company.company_id,
                            EventCompanyImpact.broker_instrument_id
                            == packet.company.broker_instrument_id,
                            Event.id != packet.event_id,
                            Event.first_seen_at <= packet.as_of,
                            sa.func.coalesce(Event.classified_at, Event.first_seen_at)
                            <= packet.as_of,
                            EventCompanyImpact.created_at <= packet.as_of,
                            Event.event_time < packet.event_time,
                            Event.merged_into_event_id.is_(None),
                            Event.event_type.is_not(None),
                            sa.exists(
                                sa.select(Source.id)
                                .join(EventSourceLink, EventSourceLink.source_id == Source.id)
                                .where(
                                    EventSourceLink.event_id == Event.id,
                                    EventSourceLink.created_at <= packet.as_of,
                                    Source.published_at.is_not(None),
                                    Source.published_at <= packet.as_of,
                                    Source.received_at <= packet.as_of,
                                )
                            ),
                        )
                        .order_by(Event.event_time.desc(), Event.id)
                        .limit(MAX_CANDIDATES * 2)
                    )
                ).all()
                match = "same_company_other_event_type_loose"

            unique_candidates: list[tuple[Event, EventCompanyImpact]] = []
            seen_events = set()
            for event, impact in candidates:
                if event.id in seen_events:
                    continue
                seen_events.add(event.id)
                unique_candidates.append((event, impact))
                if len(unique_candidates) == MAX_CANDIDATES:
                    break

            examples: list[dict[str, Any]] = []
            deadline_events: list[HistoricalEvent] = []
            missing_anchor: dt.datetime | None = None
            stats: dict[str, list[Decimal]] = {"pre_entry_alpha": [], "1h": [], "1d": [], "5d": []}
            excluded: Counter[str] = Counter()
            confounded: Counter[str] = Counter()
            # Stories entering on the same session share one market path, so
            # they are not independent observations.
            sessions: dict[str, set[dt.date | None]] = {name: set() for name in stats}
            candidate_rows: list[tuple[Event, list[NewsHistoryArticle] | None]] = []
            for event_id, articles in cached_candidates:
                event = await session.get(Event, event_id)
                if event is not None:
                    candidate_rows.append((event, articles))
            cached_ids = {event.id for event, _ in candidate_rows}
            if not cached_candidates:
                candidate_rows.extend(
                    (event, None)
                    for event, _impact in unique_candidates
                    if event.id not in cached_ids
                )
            if cached_candidates:
                match = historical_match
            for event, cached_articles in candidate_rows:
                if packet.company.currency is None:
                    excluded["currency_unknown"] += 1
                    continue
                source_times = (
                    [
                        (row[0], row[1])
                        for row in (
                            await session.execute(
                                sa.select(Source.published_at, Source.received_at)
                                .join(EventSourceLink, EventSourceLink.source_id == Source.id)
                                .where(
                                    EventSourceLink.event_id == event.id,
                                    EventSourceLink.created_at <= packet.as_of,
                                    Source.published_at.is_not(None),
                                    Source.published_at <= packet.as_of,
                                    Source.received_at <= packet.as_of,
                                )
                            )
                        ).all()
                    ]
                    if cached_articles is None
                    else []
                )
                published = (
                    min(
                        (
                            article.published_at
                            for article in cached_articles
                            if article.published_at
                        ),
                        default=None,
                    )
                    if cached_articles is not None
                    else min(
                        (moment for moment, _received in source_times if moment is not None),
                        default=None,
                    )
                )
                if published is None:
                    excluded["publication_missing"] += 1
                    continue
                first_received = (
                    min(received for _moment, received in source_times) if source_times else None
                )
                entry_latency = (
                    max(
                        dt.timedelta(minutes=5),
                        first_received - published + dt.timedelta(minutes=5),
                    )
                    if first_received is not None
                    else dt.timedelta(minutes=5)
                )
                start = published - dt.timedelta(days=4)
                end = min(packet.as_of, published + dt.timedelta(days=12))
                try:
                    stock = await self._bars(
                        session, packet.company.symbol, start, end, packet.as_of
                    )
                    benchmark = await self._bars(
                        session, self.benchmark_symbol, start, end, packet.as_of
                    )
                except ValueError:
                    excluded["candle_window_truncated"] += 1
                    continue
                if not stock or not benchmark:
                    excluded["candles_missing"] += 1
                    missing_anchor = missing_anchor or published
                    continue
                confounding = (
                    await session.scalars(
                        sa.select(Event.event_time)
                        .join(EventCompanyImpact, EventCompanyImpact.event_id == Event.id)
                        .where(
                            EventCompanyImpact.company_id == packet.company.company_id,
                            Event.id != event.id,
                            Event.event_time > published,
                            Event.event_time <= end,
                            Event.first_seen_at <= packet.as_of,
                            sa.func.coalesce(Event.classified_at, Event.first_seen_at)
                            <= packet.as_of,
                            Event.merged_into_event_id.is_(None),
                        )
                        .limit(50)
                    )
                ).all()
                cached_confounding = (
                    await session.scalars(
                        sa.select(NewsHistoryArticle.published_at)
                        .where(
                            NewsHistoryArticle.company_id == packet.company.company_id,
                            NewsHistoryArticle.event_id != event.id,
                            NewsHistoryArticle.published_at > published,
                            NewsHistoryArticle.published_at <= end,
                            NewsHistoryArticle.ingested_at <= packet.as_of,
                        )
                        .limit(50)
                    )
                ).all()
                outcome = compute_news_outcome(
                    NewsOutcomeInputs(
                        event_id=str(event.id),
                        published_at=published,
                        as_of=packet.as_of,
                        stock_bars=stock,
                        benchmark_bars=benchmark,
                        latency=entry_latency,
                        expected_close_time=dt.time(16, 0),
                        instrument_currency=packet.company.currency,
                        benchmark_currency="USD",
                        spread_bps=Decimal("10"),
                        # Measured and counted rather than dropped: a busy
                        # name has later company news inside almost every
                        # daily window, and excluding it would leave nothing.
                        confounding_policy=ConfoundingPolicy.FLAG,
                        confounding_publications=tuple(
                            moment
                            for moment in (*confounding, *cached_confounding)
                            if moment is not None
                        ),
                    )
                )
                if match == "same_company_and_event_type":
                    deadline_events.append(
                        HistoricalEvent(
                            canonical_id=str(event.id),
                            symbol=packet.company.symbol,
                            event_type=event.event_type or "UNKNOWN",
                            published_at=published,
                            live_received_at=first_received,
                            outcome=outcome,
                            stock_bars=stock,
                            benchmark_bars=benchmark,
                            article_count=len(cached_articles)
                            if cached_articles
                            else len(source_times),
                        )
                    )
                if outcome.status.value != "OK":
                    excluded[f"outcome_{outcome.status.value.lower()}"] += 1
                move = outcome.publication_to_entry.market_relative_return
                entry_session = (
                    outcome.entry.timestamp.astimezone(SESSION_ZONE).date()
                    if outcome.entry is not None
                    else None
                )
                if move is not None:
                    stats["pre_entry_alpha"].append(move)
                    sessions["pre_entry_alpha"].add(entry_session)
                values: dict[str, str | None] = {}
                for name in ("1h", "1d", "5d"):
                    horizon = outcome.horizon(name)
                    value = horizon.market_relative_return_net if horizon else None
                    if horizon is not None and value is None:
                        excluded[f"{name}_{horizon.status.value.lower()}"] += 1
                    if value is not None:
                        stats[name].append(value)
                        sessions[name].add(entry_session)
                        if horizon is not None and horizon.confounded:
                            confounded[name] += 1
                    values[name] = str(value) if value is not None else None
                if len(examples) < MAX_EXAMPLES:
                    examples.append(
                        {
                            "date": published.date().isoformat(),
                            "event_type": event.event_type,
                            "headline": event.title[:120],
                            "articles_grouped": len(cached_articles)
                            if cached_articles is not None
                            else len(source_times),
                            "actual_live_received_at": first_received.isoformat()
                            if first_received
                            else None,
                            "cache_imported_at": min(
                                article.ingested_at for article in cached_articles
                            ).isoformat()
                            if cached_articles
                            else None,
                            "entry_timing": "observed_live_receipt_plus_5m"
                            if first_received
                            else "simulated_publication_plus_5m",
                            "entry_delay_minutes": (
                                round(outcome.entry_delay_seconds / 60, 1)
                                if outcome.entry_delay_seconds is not None
                                else None
                            ),
                            "pre_entry_alpha": str(move) if move is not None else None,
                            "post_entry_alpha_net": values,
                            "status": outcome.status.value,
                            "flags": list(outcome.flags)[:4],
                        }
                    )

        summary = {}
        for name, found in stats.items():
            independent = len(sessions[name]) >= MIN_SUMMARY_SAMPLES
            summary[name] = {
                "n": len(found),
                "distinct_entry_sessions": len(sessions[name]),
                "with_later_company_news": confounded[name],
                "positive": sum(value > 0 for value in found),
                "flat": sum(value == 0 for value in found),
                "negative": sum(value < 0 for value in found),
                "median": str(_median(found)) if independent else None,
                "mean": str(sum(found, Decimal(0)) / len(found)) if independent else None,
            }
        deadline = derive_catalyst_deadline(
            deadline_events,
            buffer_sessions=self.deadline_buffer_sessions,
            minimum_samples=MIN_SUMMARY_SAMPLES,
        )
        return (
            self._datum(
                packet,
                {
                    "status": "descriptive_only",
                    "as_of": packet.as_of.isoformat(),
                    "evidence_weight": "contextual"
                    if len(sessions["1d"]) >= MIN_SUMMARY_SAMPLES
                    and match == "same_company_and_event_type"
                    else "sparse_or_loose_give_little_or_no_weight",
                    "match": match,
                    "candidate_events": len(candidate_rows),
                    "missing_window_anchor": missing_anchor.isoformat() if missing_anchor else None,
                    "excluded": dict(excluded),
                    "cache_coverage": coverage,
                    "summary": summary,
                    "catalyst_deadline_evidence": {
                        key: str(value) if isinstance(value, Decimal) else value
                        for key, value in asdict(deadline).items()
                    },
                    "examples": examples,
                    "cost_assumptions": {
                        "stock_round_trip_spread_proxy_bps": "10",
                        "fx": "unavailable_without_historical_rates",
                    },
                    "limitations": [
                        "selected_and_sparse_news_sample",
                        "SPY_is_not_sector_or_currency_matched",
                        "later_company_news_can_drive_windows_see_with_later_company_news",
                        "not_a_trading_signal",
                    ],
                },
            ),
        )

    @staticmethod
    def _datum(packet: ResearchPacket, data: dict[str, Any]) -> ResearchDatum:
        return ResearchDatum(
            provider="news_outcomes",
            kind="historical_news_candle_outcomes",
            as_of=packet.as_of,
            text=json.dumps(data, separators=(",", ":")),
        )
