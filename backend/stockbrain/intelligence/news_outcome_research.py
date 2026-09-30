"""Point-in-time, read-only historical news outcomes for research packets."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.intelligence.research import ResearchDatum, ResearchPacket
from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import NewsOutcomeInputs, compute_news_outcome

MAX_CANDIDATES = 24
MAX_EXAMPLES = 8
MIN_SUMMARY_SAMPLES = 10
BENCHMARK_SYMBOL = "SPY"
MAX_BARS_PER_SERIES = 20_000


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

    def __init__(self, database: Database, *, benchmark_symbol: str = BENCHMARK_SYMBOL) -> None:
        self.database = database
        self.benchmark_symbol = benchmark_symbol.upper()

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
        return [_bar(row) for row in rows]

    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
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

            # The current event is excluded. Each canonical event contributes once,
            # regardless of how many articles describe it.
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
            stats: dict[str, list[Decimal]] = {"pre_entry_alpha": [], "1h": [], "1d": [], "5d": []}
            excluded: Counter[str] = Counter()
            for event, _impact in unique_candidates:
                if packet.company.currency is None:
                    excluded["currency_unknown"] += 1
                    continue
                source_times = (
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
                published = min(
                    (moment for moment, _received in source_times if moment is not None),
                    default=None,
                )
                if published is None:
                    excluded["publication_missing"] += 1
                    continue
                first_received = min(received for _moment, received in source_times)
                entry_latency = max(
                    dt.timedelta(minutes=5),
                    first_received - published + dt.timedelta(minutes=5),
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
                        confounding_publications=tuple(
                            moment for moment in confounding if moment is not None
                        ),
                    )
                )
                if outcome.status.value != "OK":
                    excluded[f"outcome_{outcome.status.value.lower()}"] += 1
                move = outcome.publication_to_entry.market_relative_return
                if move is not None:
                    stats["pre_entry_alpha"].append(move)
                values: dict[str, str | None] = {}
                for name in ("1h", "1d", "5d"):
                    horizon = outcome.horizon(name)
                    value = horizon.market_relative_return_net if horizon else None
                    if horizon is not None and value is None:
                        excluded[f"{name}_{horizon.status.value.lower()}"] += 1
                    if value is not None:
                        stats[name].append(value)
                    values[name] = str(value) if value is not None else None
                if len(examples) < MAX_EXAMPLES:
                    examples.append(
                        {
                            "date": published.date().isoformat(),
                            "event_type": event.event_type,
                            "headline": event.title[:120],
                            "articles_grouped": len(source_times),
                            "first_received_at": first_received.isoformat(),
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

        summary = {
            name: {
                "n": len(values),
                "median": str(_median(values)) if len(values) >= MIN_SUMMARY_SAMPLES else None,
                "mean": str(sum(values, Decimal(0)) / len(values))
                if len(values) >= MIN_SUMMARY_SAMPLES
                else None,
            }
            for name, values in stats.items()
        }
        return (
            self._datum(
                packet,
                {
                    "status": "descriptive_only",
                    "as_of": packet.as_of.isoformat(),
                    "match": match,
                    "candidate_events": len(unique_candidates),
                    "excluded": dict(excluded),
                    "cache_coverage": coverage,
                    "summary": summary,
                    "examples": examples,
                    "cost_assumptions": {
                        "stock_round_trip_spread_proxy_bps": "10",
                        "fx": "unavailable_without_historical_rates",
                    },
                    "limitations": [
                        "selected_and_sparse_news_sample",
                        "SPY_is_not_sector_or_currency_matched",
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
