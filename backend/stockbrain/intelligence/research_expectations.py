"""What others expect and are doing, as bounded research context.

:mod:`~stockbrain.intelligence.research_data` supplies what *happened* -- completed
bars, filed financial statements, published rate levels. This module supplies what
is *expected*: analyst opinion and its direction of travel, insider behaviour, and
the probabilities a liquid market assigns to the macro events an equity thesis is
written into.

The distinction matters for the debate the packet feeds. Measured over 150 runs the
bear argued from absent evidence in 148 of them and the bull conceded the same gaps
in 137, because a thin packet always supports "we do not know enough" and rarely
supports a positive case. Every source here produces a *claim* -- 53 of 58 analysts
bullish, a 92% implied chance of a cut, an officer selling into strength -- which
the other side has to answer rather than merely note the absence of.

None of these feeds is versioned. Alpaca can be asked for a completed bar, FRED for
a vintage and SEC for facts filed before a date; an analyst-ratings endpoint, a
prediction-market book and a price-target scrape only ever describe *now*. Where a
row carries its own date it is filtered against the cutoff; where the whole payload
is current-only, :func:`require_live_cutoff` refuses a historical replay outright
rather than backdating knowledge the market did not have.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from stockbrain.db.base import utcnow
from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research import ResearchDatum, ResearchPacket

__all__ = [
    "FINNHUB_INSIDER_LIMIT",
    "POLYMARKET_TOPICS",
    "FinnhubExpectationsProvider",
    "PolymarketMacroProvider",
    "YFinanceTargetsProvider",
    "require_live_cutoff",
]

#: How far behind "now" an analysis cutoff may sit before a current-only feed is
#: refused. A live run sets ``as_of`` to the moment it starts, so this is slack
#: for a queued job rather than a licence to replay history.
LIVE_CUTOFF_TOLERANCE = dt.timedelta(hours=12)

#: Insider rows kept, newest first. Enough to show a pattern -- a cluster of sales
#: on one day is the signal -- without turning the packet into a filing dump.
FINNHUB_INSIDER_LIMIT = 12

#: Ratings periods kept, newest first. Direction of revision is the point.
FINNHUB_RATINGS_PERIODS = 6

#: Earnings surprises kept, newest first.
FINNHUB_EARNINGS_PERIODS = 4

#: The macro questions worth pricing, as a fixed catalogue rather than a search of
#: whatever is trending -- the same discipline as FRED's ``ALLOWED_SERIES``.
#:
#: Macro only, deliberately. Single-stock prediction markets exist and are excluded:
#: a market on "will NVDA close above X" is the crowd's forecast of exactly what the
#: research run is trying to forecast, so importing it drags the thesis toward
#: consensus at the precise point where an edge would have to come from departing
#: from it. The rate path and recession odds are different -- they are backdrop the
#: system does not forecast and currently guesses at.
POLYMARKET_TOPICS: tuple[str, ...] = (
    "fed rate",
    "CPI",
    "recession",
)

#: Markets kept per topic, by volume. Thin books price badly.
POLYMARKET_PER_TOPIC = 3

#: Below this, a market's price is a couple of trades rather than a forecast.
POLYMARKET_MIN_VOLUME = 10_000.0

#: Outcome buckets kept per event. A rate-path question can carry a dozen.
POLYMARKET_MAX_OUTCOMES = 12


def require_live_cutoff(
    as_of: dt.datetime, *, now: dt.datetime | None = None, tolerance: dt.timedelta | None = None
) -> None:
    """Refuse a current-only feed when the analysis cutoff is materially past.

    These endpoints have no vintage parameter: they answer for today whatever date
    you ask about. Attaching today's analyst ratings to a cutoff three days ago
    would let a thesis cite an upgrade published after the moment it claims to
    reason from -- the same lookahead the completed-bar filter and the FRED vintage
    exist to prevent, arriving through a door that has no lock on it.
    """
    moment = now or utcnow()
    limit = tolerance or LIVE_CUTOFF_TOLERANCE
    if moment - as_of > limit:
        raise ProviderResponseError(
            "expectations data is current-only and cannot be served for a historical cutoff"
        )


def _as_date(value: object) -> str | None:
    """The leading ``YYYY-MM-DD`` of a provider date, or ``None`` if unusable."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    candidate = value[:10]
    try:
        dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


class FinnhubExpectationsProvider:
    """Analyst ratings, insider transactions and earnings surprises.

    Three endpoints, each filtered against the packet cutoff on the date the
    provider itself supplies -- the ratings ``period``, the Form 4 ``filingDate``
    and the earnings ``period``. Filing date rather than transaction date on
    purpose: an insider sale becomes knowable when the form is filed, not when the
    trade happened, and the gap between the two is routinely days.

    A failing endpoint degrades to an empty list named in ``unavailable`` rather
    than failing the whole datum: the three are independent, and losing earnings
    history is no reason to discard the analyst trend.
    """

    def __init__(self, http: ProviderHttpClient, *, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def _get(self, path: str, symbol: str) -> Any:
        return await self._http.get_json(path, params={"symbol": symbol, "token": self._key})

    async def context(
        self, packet: ResearchPacket, *, now: dt.datetime | None = None
    ) -> tuple[ResearchDatum, ...]:
        if not self._key:
            raise ProviderResponseError("finnhub: API key missing")
        require_live_cutoff(packet.as_of, now=now)
        cutoff = packet.as_of.date().isoformat()
        symbol = packet.company.symbol
        unavailable: list[str] = []

        ratings = await self._ratings(symbol, cutoff, unavailable)
        insider = await self._insider(symbol, cutoff, unavailable)
        earnings = await self._earnings(symbol, cutoff, unavailable)
        if not ratings["history"] and not insider["recent"] and not earnings:
            raise ProviderResponseError("finnhub: no expectations data at or before the cutoff")

        return (
            ResearchDatum(
                provider="finnhub",
                kind="analyst_insider_and_earnings",
                as_of=packet.as_of,
                text=json.dumps(
                    {
                        "symbol": symbol,
                        "knowable_on_or_before": cutoff,
                        "analyst_ratings": ratings,
                        "insider": insider,
                        "earnings_surprises": earnings,
                        "unavailable": unavailable,
                        "execution_pricing": False,
                    }
                ),
            ),
        )

    async def _ratings(self, symbol: str, cutoff: str, unavailable: list[str]) -> dict[str, Any]:
        try:
            payload = await self._get("/stock/recommendation", symbol)
        except ProviderError:
            unavailable.append("analyst_ratings")
            return {"latest": None, "history": [], "total_analysts": 0, "trend": None}
        rows = [
            row
            for row in (payload if isinstance(payload, list) else [])
            if isinstance(row, dict) and (_as_date(row.get("period")) or "9999") <= cutoff
        ]
        rows.sort(key=lambda row: str(row.get("period")), reverse=True)
        rows = rows[:FINNHUB_RATINGS_PERIODS]
        if not rows:
            unavailable.append("analyst_ratings")
            return {"latest": None, "history": [], "total_analysts": 0, "trend": None}

        def bullish_share(row: dict[str, Any]) -> float | None:
            counts: list[int] = []
            for key in ("strongBuy", "buy", "hold", "sell", "strongSell"):
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                counts.append(value)
            total = sum(counts)
            if total <= 0:
                return None
            return round((counts[0] + counts[1]) / total, 4)

        latest, oldest = rows[0], rows[-1]
        newest_share, oldest_share = bullish_share(latest), bullish_share(oldest)
        trend = None
        if newest_share is not None and oldest_share is not None and len(rows) > 1:
            trend = {
                "bullish_share_latest": newest_share,
                "bullish_share_earliest": oldest_share,
                "direction": (
                    "improving"
                    if newest_share > oldest_share
                    else "deteriorating"
                    if newest_share < oldest_share
                    else "flat"
                ),
                "periods_compared": len(rows),
            }
        total = sum(
            value
            for key, value in latest.items()
            if key in ("strongBuy", "buy", "hold", "sell", "strongSell") and isinstance(value, int)
        )
        return {
            "latest": {
                key: latest.get(key)
                for key in ("period", "strongBuy", "buy", "hold", "sell", "strongSell")
            },
            "history": [
                {
                    key: row.get(key)
                    for key in ("period", "strongBuy", "buy", "hold", "sell", "strongSell")
                }
                for row in rows
            ],
            "total_analysts": total,
            "trend": trend,
        }

    async def _insider(self, symbol: str, cutoff: str, unavailable: list[str]) -> dict[str, Any]:
        note = (
            "SEC Form 4 insider reporting applies to US domestic registrants only. "
            "An empty list for a foreign private issuer means the obligation does "
            "not exist, not that data is missing."
        )
        try:
            payload = await self._get("/stock/insider-transactions", symbol)
        except ProviderError:
            unavailable.append("insider")
            return {"recent": [], "note": note}
        rows = payload.get("data") if isinstance(payload, dict) else None
        eligible = [
            {
                "name": row.get("name"),
                "code": row.get("transactionCode"),
                "change": row.get("change"),
                "price": row.get("transactionPrice"),
                "traded": _as_date(row.get("transactionDate")),
                "filed": filed,
            }
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
            and (filed := _as_date(row.get("filingDate")))
            and filed <= cutoff
        ]
        eligible.sort(key=lambda row: (str(row["filed"]), str(row["traded"])), reverse=True)
        return {"recent": eligible[:FINNHUB_INSIDER_LIMIT], "note": note}

    async def _earnings(
        self, symbol: str, cutoff: str, unavailable: list[str]
    ) -> list[dict[str, Any]]:
        try:
            payload = await self._get("/stock/earnings", symbol)
        except ProviderError:
            unavailable.append("earnings_surprises")
            return []
        rows = [
            {
                "period": period,
                "estimate": row.get("estimate"),
                "actual": row.get("actual"),
                "surprise_percent": row.get("surprisePercent"),
            }
            for row in (payload if isinstance(payload, list) else [])
            if isinstance(row, dict)
            and (period := _as_date(row.get("period")))
            and period <= cutoff
        ]
        rows.sort(key=lambda row: str(row["period"]), reverse=True)
        if not rows:
            unavailable.append("earnings_surprises")
        return rows[:FINNHUB_EARNINGS_PERIODS]


class PolymarketMacroProvider:
    """Market-implied probabilities for the macro events a thesis is written into.

    The packet already carries FRED's *level* -- what the policy rate is. What the
    manager reports kept reaching for and never had is the forward distribution:
    "hawkish into two binary events", "pending the Sept 15-16 decision". A liquid
    prediction market prices exactly that, needs no key, and is the only free
    source of it.

    Keyless and unauthenticated, so it is also the least trustworthy input here:
    the volume floor and the fixed topic catalogue are what stop a thin, promoted
    or joke market reaching an analyst as though it were a forecast.
    """

    def __init__(self, http: ProviderHttpClient) -> None:
        self._http = http

    async def context(
        self, as_of: dt.datetime, *, now: dt.datetime | None = None
    ) -> tuple[ResearchDatum, ...]:
        require_live_cutoff(as_of, now=now)
        markets: list[dict[str, Any]] = []
        for topic in POLYMARKET_TOPICS:
            try:
                payload = await self._http.get_json(
                    "/public-search", params={"q": topic, "limit_per_type": 20}
                )
            except ProviderError:
                continue
            markets.extend(self._forward_looking(payload, topic, as_of))
        if not markets:
            raise ProviderResponseError("polymarket: no liquid forward-looking macro markets")
        return (
            ResearchDatum(
                provider="polymarket",
                kind="macro_event_probabilities",
                as_of=as_of,
                text=json.dumps(
                    {
                        "note": (
                            "Crowd-implied probabilities for macro events, priced at "
                            "fetch time. Context for the rate and growth backdrop only; "
                            "not a forecast of any individual security."
                        ),
                        "topics": list(POLYMARKET_TOPICS),
                        "markets": markets,
                        "execution_pricing": False,
                    }
                ),
            ),
        )

    @staticmethod
    def _distribution(markets: object) -> list[dict[str, Any]]:
        """The whole outcome distribution, each bucket under its own label.

        A Polymarket *event* is a set of sibling binary markets, one per bucket:
        "How many Fed rate cuts in 2026?" is thirteen markets, the first being
        "Will NO Fed rate cuts happen in 2026?". Reading only ``markets[0]`` and
        reporting its "Yes" price against the *event* title inverts the meaning
        outright -- 0.9265 is a 93% chance of **no** cuts, and would reach the
        analyst as a 93% chance of cuts. Each bucket therefore carries its own
        ``groupItemTitle``, and the probability quoted is that bucket's "Yes".
        """
        rows: list[dict[str, Any]] = []
        for market in markets if isinstance(markets, list) else []:
            if not isinstance(market, dict):
                continue
            try:
                prices = json.loads(market.get("outcomePrices") or "[]")
                names = json.loads(market.get("outcomes") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if len(prices) != len(names) or not prices:
                continue
            label = market.get("groupItemTitle") or market.get("question")
            probability = next(
                (price for name, price in zip(names, prices, strict=True) if str(name) == "Yes"),
                None,
            )
            if label is None or probability is None:
                continue
            rows.append({"outcome": str(label)[:60], "probability": probability})
        return rows[:POLYMARKET_MAX_OUTCOMES]

    @staticmethod
    def _forward_looking(payload: object, topic: str, as_of: dt.datetime) -> list[dict[str, Any]]:
        events = payload.get("events") if isinstance(payload, dict) else None
        out: list[dict[str, Any]] = []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            ends = _as_date(event.get("endDate"))
            # A market that already resolved is a historical fact wearing a
            # probability's clothes.
            if not ends or ends <= as_of.date().isoformat():
                continue
            try:
                volume = float(event.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < POLYMARKET_MIN_VOLUME:
                continue
            outcomes = PolymarketMacroProvider._distribution(event.get("markets"))
            if not outcomes:
                continue
            out.append(
                {
                    "topic": topic,
                    "question": str(event.get("title"))[:200],
                    "ends": ends,
                    "volume_usd": round(volume, 2),
                    "implied": outcomes,
                }
            )
        out.sort(key=lambda row: row["volume_usd"], reverse=True)
        return out[:POLYMARKET_PER_TOPIC]


#: Fields lifted from a Yahoo quote summary. An explicit list, because ``.info``
#: is a large untyped grab-bag whose shape Yahoo changes without notice.
YF_TARGET_FIELDS: tuple[tuple[str, str], ...] = (
    ("target_mean", "targetMeanPrice"),
    ("target_high", "targetHighPrice"),
    ("target_low", "targetLowPrice"),
    ("target_median", "targetMedianPrice"),
    ("analyst_count", "numberOfAnalystOpinions"),
    ("recommendation", "recommendationKey"),
    ("recommendation_mean", "recommendationMean"),
    ("forward_eps", "forwardEps"),
    ("trailing_eps", "trailingEps"),
    ("forward_pe", "forwardPE"),
)


class YFinanceTargetsProvider:
    """Analyst price targets and forward estimates, from Yahoo via ``yfinance``.

    The one source here with no contract behind it. ``yfinance`` rides Yahoo's
    undocumented quote endpoints: no key, no SLA, no stability guarantee, and it
    breaks whenever Yahoo reshapes a payload. It earns its place because it is the
    only free source of price targets and forward EPS that covers the foreign
    listings every other free feed refuses -- ASML and VWAGY return targets here
    and nowhere else.

    Consequently it is built to be the one that fails. Every call is wrapped, the
    field list is explicit rather than "whatever ``.info`` returned", and any
    failure raises :class:`ProviderResponseError` so ``_enrich`` degrades this one
    provider and keeps the rest of the packet. Nothing downstream may depend on it.

    ``yfinance`` is synchronous and does blocking network I/O, so it runs on a
    worker thread; calling it directly would stall the event loop for every other
    job in the process.
    """

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self._timeout = timeout_seconds

    @staticmethod
    def _numeric(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        number = float(value)
        return round(number, 6) if number == number and abs(number) != float("inf") else None

    def _fetch(self, symbol: str) -> dict[str, Any]:
        import yfinance  # imported here so a broken Yahoo dependency cannot stop startup

        info = yfinance.Ticker(symbol).info or {}
        if not isinstance(info, dict):
            raise ProviderResponseError("yfinance: quote summary was not an object")
        out: dict[str, Any] = {}
        for name, key in YF_TARGET_FIELDS:
            raw = info.get(key)
            out[name] = (
                raw if name == "recommendation" and isinstance(raw, str) else self._numeric(raw)
            )
        return out

    async def context(
        self, packet: ResearchPacket, *, now: dt.datetime | None = None
    ) -> tuple[ResearchDatum, ...]:
        import asyncio

        require_live_cutoff(packet.as_of, now=now)
        symbol = packet.company.symbol
        try:
            fields = await asyncio.wait_for(
                asyncio.to_thread(self._fetch, symbol), timeout=self._timeout
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderResponseError(
                f"yfinance: quote summary unavailable ({type(exc).__name__})"
            ) from None
        if fields.get("target_mean") is None and fields.get("analyst_count") is None:
            raise ProviderResponseError(f"yfinance: no analyst coverage for {symbol}")
        return (
            ResearchDatum(
                provider="yfinance",
                kind="analyst_targets_and_forward_estimates",
                as_of=packet.as_of,
                text=json.dumps(
                    {
                        "symbol": symbol,
                        "note": (
                            "Consensus analyst targets and forward estimates as published "
                            "at fetch time. Unofficial source, research context only; "
                            "never an execution reference."
                        ),
                        **fields,
                        "execution_pricing": False,
                    }
                ),
            ),
        )
