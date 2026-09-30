"""Explicit bounded cache backfill and read-only live-trade diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from dataclasses import asdict

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.ingestion.alpaca_news import AlpacaNewsClient
from stockbrain.intelligence.news_history import BackfillLimits, NewsHistoryCache
from stockbrain.intelligence.news_trade_diagnostics import (
    render_news_trade_diagnostics,
    run_news_trade_diagnostics,
)
from stockbrain.market_data.alpaca import AlpacaMarketDataClient


def _aware(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return moment.astimezone(dt.UTC)


async def _run(args: argparse.Namespace) -> None:
    # The target database must be selected explicitly. Reading credentials from
    # Settings does not change any runtime setting or broker execution policy.
    settings = Settings(database_url=args.database_url)
    database = Database(settings)
    try:
        if args.mode == "report":
            report = await run_news_trade_diagnostics(database, as_of=args.as_of)
            print(render_news_trade_diagnostics(report))  # noqa: T201
            return
        cache = NewsHistoryCache(database)
        limits = BackfillLimits()  # Fixed caps; repeat an invocation for another window.
        if args.mode == "news":
            client = AlpacaNewsClient(settings)
            try:
                result = await cache.backfill_news(
                    start=args.start,
                    end=args.end,
                    symbols=args.symbols,
                    limits=limits,
                    client=client,
                )
                print(json.dumps(asdict(result), default=str))  # noqa: T201
            finally:
                await client.aclose()
        else:
            provider = AlpacaMarketDataClient(settings)
            try:
                for symbol in args.symbols:
                    result = await cache.backfill_candles(
                        symbol=symbol,
                        start=args.start,
                        end=args.end,
                        limits=limits,
                        provider=provider,
                    )
                    print(json.dumps(asdict(result), default=str))  # noqa: T201
            finally:
                await provider.aclose()
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("news", "candles", "report"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--start", type=_aware)
    parser.add_argument("--end", type=_aware)
    parser.add_argument("--as-of", type=_aware)
    parser.add_argument("--symbol", dest="symbols", action="append", default=[])
    args = parser.parse_args()
    if args.mode == "report":
        if args.as_of is None:
            parser.error("report requires --as-of")
    elif args.start is None or args.end is None or not args.symbols:
        parser.error("backfill requires --start, --end and at least one --symbol")
    if len(args.symbols) > 5:
        parser.error("at most five symbols per invocation")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
