"""Explicit research providers: FRED macro, Phase 4 Alpaca context, SEC XBRL facts."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from stockbrain.db.base import utcnow
from stockbrain.enums import BarTimeframe
from stockbrain.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.intelligence.research import ResearchDatum, ResearchPacket
from stockbrain.market_data.base import Bar, MarketDataProvider, ProviderCapability, Quote, Trade
from stockbrain.market_data.reaction import PriceReactionCalculator


class Observation(BaseModel):
    date: dt.date
    value: str
    realtime_start: dt.date
    realtime_end: dt.date


class Observations(BaseModel):
    observations: list[Observation] = Field(max_length=100)


def fred_error(response: httpx.Response, error: Exception) -> Exception:
    if response.status_code == 400 and "api_key" in response.text.lower():
        return ProviderAuthError("fred: API key rejected")
    if response.status_code == 423:
        return ProviderUnavailable("fred: resource locked")
    if isinstance(error, ProviderResponseError):
        return ProviderResponseError(f"fred: invalid response (HTTP {response.status_code})")
    return error


class FredMacroProvider:
    # DFF measures policy rates; DGS10 measures long-term rates. No catalogue scan.
    ALLOWED_SERIES = frozenset({"DFF", "DGS10"})

    def __init__(
        self,
        key: SecretStr,
        *,
        series: tuple[str, ...] = ("DFF", "DGS10"),
        http: ProviderHttpClient | None = None,
    ) -> None:
        if not set(series) <= self.ALLOWED_SERIES or not series:
            raise ValueError("unsupported FRED research series")
        self.key = key
        self.series = series
        self.http = http or ProviderHttpClient(
            provider="fred",
            base_url="https://api.stlouisfed.org/fred/",
            rate_limiter=TokenBucket(1, burst=1),
            max_attempts=2,
            refine_error=fred_error,
        )

    async def context(self, as_of: dt.datetime) -> tuple[ResearchDatum, ...]:
        if not self.key.get_secret_value():
            raise ProviderAuthError("fred: API key missing")
        # FRED's vintage granularity is a day. Use the prior Chicago day so an
        # intraday replay cannot see revisions released later on the analysis day.
        vintage = min(as_of, utcnow()).astimezone(
            ZoneInfo("America/Chicago")
        ).date() - dt.timedelta(days=1)
        start = vintage - dt.timedelta(days=90)
        results = []
        for series_id in self.series:
            payload = await self.http.get_json(
                "series/observations",
                params={
                    "api_key": self.key.get_secret_value(),
                    "series_id": series_id,
                    "file_type": "json",
                    "observation_start": start.isoformat(),
                    "observation_end": vintage.isoformat(),
                    "realtime_start": vintage.isoformat(),
                    "realtime_end": vintage.isoformat(),
                    "units": "lin",
                    "sort_order": "desc",
                    "limit": 100,
                },
            )
            try:
                parsed = Observations.model_validate(payload)
                values = []
                for row in parsed.observations:
                    if (
                        not start <= row.date <= vintage
                        or not row.realtime_start <= vintage <= row.realtime_end
                    ):
                        raise ValueError("observation outside vintage")
                    value = None if row.value == "." else Decimal(row.value)
                    if value is not None and not value.is_finite():
                        raise ValueError("non-finite observation")
                    values.append(
                        {
                            "date": row.date.isoformat(),
                            "value": str(value) if value is not None else None,
                        }
                    )
            except (ValidationError, ValueError, InvalidOperation):
                raise ProviderResponseError("fred: malformed observations") from None
            results.append(
                ResearchDatum(
                    provider="fred",
                    kind=series_id,
                    as_of=as_of,
                    text=json.dumps(
                        {
                            "series_id": series_id,
                            "vintage": vintage.isoformat(),
                            "observations": values,
                        }
                    ),
                )
            )
        return tuple(results)

    async def aclose(self) -> None:
        await self.http.aclose()


class AlpacaResearchProvider:
    """Research snapshot from the existing market provider, without an SDK fallback."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
        bars = await self.provider.bars(
            packet.company.symbol,
            BarTimeframe.DAY_1,
            packet.as_of - dt.timedelta(days=365),
            packet.as_of,
            limit=260,
        )
        # Daily bars are timestamped at their opening. Exclude the analysis day
        # entirely, since an in-progress daily close is not historical evidence.
        eligible = sorted(
            (bar for bar in bars if bar.timestamp + dt.timedelta(days=1) <= packet.as_of),
            key=lambda bar: bar.timestamp,
        )
        if not eligible:
            raise ProviderResponseError("alpaca research: no completed historical bars")
        eligible = eligible[-180:]
        # A columnar encoding rather than 180 repetitions of the same seven keys.
        # Identical information, roughly a third of the characters: the bars were
        # 66% of the whole packet and were crowding out the article the run
        # exists to analyse. The header ships with the rows so the schema is
        # still self-describing to a reader that has never seen this format.
        rows = [
            ",".join(
                (
                    bar.timestamp.date().isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                )
            )
            for bar in eligible
        ]
        reaction = await PriceReactionCalculator(_AsOfMarket(self.provider, packet.as_of)).compute(
            packet.company.symbol, packet.event_time, now=packet.as_of
        )
        return (
            ResearchDatum(
                provider="alpaca",
                kind="price_history_and_event_reaction",
                as_of=packet.as_of,
                text=json.dumps(
                    {
                        "symbol": packet.company.symbol,
                        "timeframe": BarTimeframe.DAY_1.value,
                        "bars_schema": "date,open,high,low,close,volume",
                        "bars": rows,
                        "bars_feed": sorted({bar.feed for bar in eligible if bar.feed}),
                        "reaction": reaction.as_dict(),
                        "execution_pricing": False,
                    }
                ),
            ),
        )


class _AsOfMarket:
    """Give Phase 4's reaction calculator only completed bars at the cutoff."""

    def __init__(self, provider: MarketDataProvider, as_of: dt.datetime) -> None:
        self.provider = provider
        self.name = provider.name
        self.as_of = as_of

    async def latest_quote(self, symbol: str) -> Quote:
        raise ProviderResponseError("research uses a completed historical bar reference")

    async def latest_trade(self, symbol: str) -> Trade:
        raise ProviderResponseError("research does not request latest trades")

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> list[Bar]:
        try:
            bars = await self.provider.bars(
                symbol, timeframe, start, min(end, self.as_of), limit=limit
            )
        except ProviderError as exc:
            # Phase 4 includes exception text in reaction notes. Keep provider
            # bodies out of this packet while retaining the concrete error type.
            raise type(exc)("research bars unavailable") from None
        return sorted(
            (
                bar
                for bar in bars
                if start <= bar.timestamp
                and bar.timestamp + dt.timedelta(minutes=1) <= min(end, self.as_of)
            ),
            key=lambda bar: bar.timestamp,
        )

    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        raise ProviderResponseError("research snapshot is not an execution capability")

    async def aclose(self) -> None:
        pass


#: The concepts a fundamentals analyst is allowed to ask for, in the order they
#: are reported.  An explicit catalogue rather than a catalogue scan, for the
#: same reason :class:`FredMacroProvider` has one: a bounded, inspectable list
#: cannot grow into an unbounded crawl of a provider's whole taxonomy.
#:
#: Each concept lists ``(taxonomy, tag)`` candidates and the first that returns
#: usable facts wins.  Two reasons there is more than one:
#:
#: * US GAAP renamed things and filers did not migrate in lockstep, so revenue
#:   is spelled three ways across otherwise comparable 10-Ks;
#: * a foreign private issuer files 20-F under **IFRS**, whose tags live in the
#:   ``ifrs-full`` taxonomy entirely.  TSMC is the worked example: it resolves to
#:   a valid CIK, files with the SEC, and has no ``us-gaap`` facts at all.
#:   Without the IFRS rung every IFRS-reporting ADR degrades to "no data".
SEC_CONCEPTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "revenue",
        (
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "Revenues"),
            ("us-gaap", "SalesRevenueNet"),
            ("ifrs-full", "Revenue"),
            ("ifrs-full", "RevenueFromContractsWithCustomers"),
        ),
    ),
    ("net_income", (("us-gaap", "NetIncomeLoss"), ("ifrs-full", "ProfitLoss"))),
    ("assets", (("us-gaap", "Assets"), ("ifrs-full", "Assets"))),
    ("liabilities", (("us-gaap", "Liabilities"), ("ifrs-full", "Liabilities"))),
    ("equity", (("us-gaap", "StockholdersEquity"), ("ifrs-full", "Equity"))),
    (
        "operating_cash_flow",
        (
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
        ),
    ),
    (
        "diluted_eps",
        (
            ("us-gaap", "EarningsPerShareDiluted"),
            ("ifrs-full", "DilutedEarningsLossPerShare"),
        ),
    ),
)

#: Observations kept per concept, newest first.  Enough to show a trend and a
#: year-on-year comparison; not enough to become a data dump.
SEC_MAX_OBSERVATIONS = 6

#: Only periodic reports.  An 8-K carries no XBRL financial statements worth
#: comparing, and including every form would turn the trend into noise.
SEC_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F"})


class SecFilingSource(Protocol):
    """The slice of :class:`~stockbrain.ingestion.sec_edgar.SecEdgarClient` this
    provider needs.  Depending on the two methods rather than the class keeps the
    research package free of an ingestion import at type-check time and makes the
    provider testable without an HTTP transport."""

    async def ticker_to_cik(self) -> dict[str, str]: ...

    async def company_concept(
        self, cik: str | int, tag: str, *, taxonomy: str = "us-gaap"
    ) -> dict[str, Any] | None: ...


class SecXbrlFundamentalsProvider:
    """Company financial facts from SEC XBRL, as *research* context only.

    Free and keyless: the SEC asks for a descriptive User-Agent, which the
    existing EDGAR client already sends, and rate-limits per IP, which the token
    bucket that client owns already respects.  Taking that client rather than a
    fresh HTTP session is what keeps ingestion and research on one rate budget --
    two independent clients under one IP would race each other into a block.

    Point-in-time is enforced on ``filed``, not on ``end``.  A quarter that ended
    before the analysis instant but was *reported* after it was not knowable at
    the cutoff, and letting it through would backdate a number the market had
    not seen -- the same mistake the FRED vintage and the completed-bar filter
    exist to prevent.
    """

    def __init__(
        self,
        client: SecFilingSource,
        *,
        ticker_map: dict[str, str] | None = None,
        concepts: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = SEC_CONCEPTS,
    ) -> None:
        self._client = client
        self._ticker_map = ticker_map
        self._map_lock = asyncio.Lock()
        self._concepts = concepts

    async def _cik_for(self, symbol: str) -> str:
        if self._ticker_map is None:
            async with self._map_lock:
                if self._ticker_map is None:
                    self._ticker_map = await self._load_ticker_map()
        cik = self._ticker_map.get(symbol.upper())
        if cik is None:
            # Not an outage: most non-US listings simply do not file with the
            # SEC.  It still degrades this provider rather than inventing a gap
            # the fundamentals analyst would have to guess about.
            raise ProviderResponseError(f"sec: no CIK is mapped to {symbol.upper()}")
        return cik

    async def _load_ticker_map(self) -> dict[str, str]:
        """EDGAR's symbol index, fetched once per process and held.

        It is a megabyte of rarely-changing reference data; re-fetching it per
        research run would spend the shared EDGAR rate budget on an answer that
        did not change.
        """
        mapping = await self._client.ticker_to_cik()
        if not mapping:
            raise ProviderResponseError("sec: company_tickers.json carried no usable rows")
        return mapping

    @staticmethod
    def _observations(payload: dict[str, Any], cutoff: dt.date) -> tuple[str, list[dict[str, Any]]]:
        units = payload.get("units")
        if not isinstance(units, dict) or not units:
            return "", []
        # Deterministic rather than "whatever key JSON happened to put first":
        # monetary concepts report in USD, per-share ones in USD/shares, and a
        # filer with a second reporting currency must not silently change which
        # series the analyst compares year on year.
        unit = next(
            (candidate for candidate in ("USD", "USD/shares") if candidate in units),
            sorted(units)[0],
        )
        entries = units.get(unit)
        if not isinstance(entries, list):
            return "", []
        eligible = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("form") not in SEC_FORMS:
                continue
            filed, end, value = entry.get("filed"), entry.get("end"), entry.get("val")
            if not isinstance(filed, str) or not isinstance(end, str):
                continue
            if not isinstance(value, int | float) or isinstance(value, bool):
                continue
            try:
                filed_on = dt.date.fromisoformat(filed)
            except ValueError:
                continue
            if filed_on > cutoff:
                continue
            eligible.append(
                {
                    "end": end,
                    "val": value,
                    "form": entry.get("form"),
                    "filed": filed,
                    "fy": entry.get("fy"),
                    "fp": entry.get("fp"),
                }
            )
        # Newest reporting period first, then newest filing, so a restatement
        # supersedes the original it corrects.
        eligible.sort(key=lambda row: (row["end"], row["filed"]), reverse=True)
        return unit, eligible[:SEC_MAX_OBSERVATIONS]

    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]:
        cik = await self._cik_for(packet.company.symbol)
        cutoff = packet.as_of.date()
        concepts: dict[str, Any] = {}
        for name, candidates in self._concepts:
            for taxonomy, tag in candidates:
                payload = await self._client.company_concept(cik, tag, taxonomy=taxonomy)
                if payload is None:
                    continue
                unit, observations = self._observations(payload, cutoff)
                if not observations:
                    continue
                concepts[name] = {
                    "taxonomy": taxonomy,
                    "tag": tag,
                    "unit": unit,
                    "observations": observations,
                }
                break
        if not concepts:
            raise ProviderResponseError(
                f"sec: no XBRL facts for CIK{cik} were filed on or before {cutoff.isoformat()}"
            )
        return (
            ResearchDatum(
                provider="sec",
                kind="xbrl_company_facts",
                as_of=packet.as_of,
                text=json.dumps(
                    {
                        "symbol": packet.company.symbol,
                        "cik": cik,
                        "filed_on_or_before": cutoff.isoformat(),
                        "concepts": concepts,
                        "missing": [name for name, _ in self._concepts if name not in concepts],
                        "execution_pricing": False,
                    }
                ),
            ),
        )
