"""SEC EDGAR filing discovery.

Verified against SEC documentation (2026-09-04):

* no API key; ``data.sec.gov`` and ``www.sec.gov`` both require a descriptive
  ``User-Agent`` containing a contact address, or they return 403
* **the rate limit is 10 requests/second per IP across all EDGAR domains**, and
  exceeding it earns a 403 plus an IP block of roughly ten minutes. That is the
  single most important operational fact about this provider, and it is enforced
  here by a client-side token bucket set well below the ceiling rather than
  discovered by being blocked.
* submissions: ``https://data.sec.gov/submissions/CIK##########.json`` -- CIK
  zero-padded to ten digits. ``filings.recent`` is a *columnar* structure:
  parallel arrays, not a list of objects.
* ticker map: ``https://www.sec.gov/files/company_tickers.json``
* daily index: ``https://www.sec.gov/Archives/edgar/daily-index/...``

Filings are authoritative but not directional: an 8-K is not inherently bullish
or bearish, and nothing in this module pretends otherwise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from typing import Any

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "PRIORITY_FORMS",
    "SEC_MAX_REQUESTS_PER_SECOND",
    "SecEdgarClient",
    "normalize_cik",
    "parse_recent_filings",
]

log = get_logger(__name__)

#: SEC's documented ceiling. The client runs below it deliberately: the penalty
#: for exceeding it is a ten-minute IP block, not a 429 we could back off from.
SEC_MAX_REQUESTS_PER_SECOND = 10.0
_CLIENT_REQUESTS_PER_SECOND = 5.0

#: Forms worth reacting to. Everything else is ingested only when explicitly
#: requested, to keep classifier spend proportionate.
PRIORITY_FORMS: frozenset[str] = frozenset(
    {"8-K", "10-Q", "10-K", "6-K", "20-F", "SC 13D", "SC 13G", "4", "S-1", "424B4", "425"}
)

_FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"
_FILING_PAGE_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"
)


def normalize_cik(cik: str | int) -> str:
    """Zero-pad a CIK to the ten digits the submissions endpoint requires."""
    digits = "".join(character for character in str(cik) if character.isdigit())
    if not digits:
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def parse_recent_filings(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Flatten ``filings.recent`` from columnar arrays into per-filing dicts.

    SEC returns parallel arrays (``form[i]`` describes the same filing as
    ``accessionNumber[i]``). Rows are only emitted while *every* required column
    has a value at that index, so a truncated column cannot silently produce a
    filing with mismatched fields.
    """
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        return
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        return

    required = ("accessionNumber", "form", "filingDate")
    optional = (
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "primaryDocDescription",
        "items",
        "isXBRL",
        "size",
    )

    columns = {name: recent.get(name) for name in (*required, *optional)}
    for name in required:
        if not isinstance(columns[name], list):
            return

    length = min(len(columns[name]) for name in required)  # type: ignore[arg-type]
    for index in range(length):
        row: dict[str, Any] = {}
        for name in (*required, *optional):
            column = columns[name]
            if isinstance(column, list) and index < len(column):
                row[name] = column[index]
        yield row


class SecEdgarClient:
    """Filing discovery for watchlisted companies and for the global feed."""

    name = "sec"

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._user_agent = self.build_user_agent(settings)
        shared_bucket = TokenBucket(
            rate_per_second=_CLIENT_REQUESTS_PER_SECOND, burst=int(_CLIENT_REQUESTS_PER_SECOND)
        )
        self._http = http or ProviderHttpClient(
            provider="sec",
            base_url=settings.sec_base_url,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            },
            timeout_seconds=30.0,
            rate_limiter=shared_bucket,
        )
        self._www = ProviderHttpClient(
            provider="sec",
            base_url=settings.sec_www_base_url,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout_seconds=30.0,
            # Same bucket: the SEC limit is per IP across all EDGAR domains, so
            # two clients sharing one process must share one budget.
            rate_limiter=shared_bucket,
        )

    @staticmethod
    def build_user_agent(settings: Settings) -> str:
        """SEC requires a descriptive agent with a contact, or it returns 403."""
        contact = settings.sec_contact_email.strip() or "unset"
        return f"{settings.app_name}/{settings.app_version} (contact: {contact})"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._www.aclose()

    # ------------------------------------------------------------------
    async def company_tickers(self) -> dict[str, dict[str, Any]]:
        """Fetch the official CIK-to-ticker map, keyed by padded CIK.

        This is the authoritative link between a filing and a market symbol; it
        is not a substitute for broker instrument resolution.

        One CIK routinely owns several symbols -- share classes and the
        ordinary/ADR pairs of foreign issuers -- so every one of them is kept in
        ``tickers``.  Collapsing them cost 2,407 of EDGAR's 10,412 symbols and
        left whichever sibling appeared last in the file as the company's only
        name.  ``ticker`` remains the *first* symbol listed for the CIK, which is
        stable across fetches in a way that "last one to overwrite" was not.

        Callers going the other way want :meth:`ticker_to_cik`; a symbol lookup
        must not be built by inverting this map.
        """
        payload = await self._www.get_json("/files/company_tickers.json")
        if not isinstance(payload, dict):
            raise ProviderResponseError("sec: company_tickers.json was not a JSON object")

        mapping: dict[str, dict[str, Any]] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            raw_cik = entry.get("cik_str")
            ticker = entry.get("ticker")
            title = entry.get("title")
            if raw_cik is None or not ticker:
                continue
            try:
                cik = normalize_cik(raw_cik)
            except ValueError:
                continue
            symbol = str(ticker).upper()
            existing = mapping.get(cik)
            if existing is None:
                mapping[cik] = {
                    "cik": cik,
                    "ticker": symbol,
                    "tickers": [symbol],
                    "title": title,
                }
            elif symbol not in existing["tickers"]:
                existing["tickers"].append(symbol)
        return mapping

    async def ticker_to_cik(self) -> dict[str, str]:
        """The same file as :meth:`company_tickers`, keyed the other way.

        Not derivable from that method's result.  ``company_tickers.json`` has
        one row per *ticker* and several tickers routinely share a CIK -- share
        classes (GOOGL/GOOG/GOOGN) and the ordinary/ADR pairs of foreign issuers
        (TSM/TSMWF, ASML/ASMLF).  Keying by CIK keeps one ticker per company and
        silently drops the rest: 10,412 rows collapse to 8,005, and which of the
        siblings survives is decided by file order.  A caller that starts from a
        symbol needs every row, so this builds the reverse index from the payload
        instead of inverting a lossy one.
        """
        payload = await self._www.get_json("/files/company_tickers.json")
        if not isinstance(payload, dict):
            raise ProviderResponseError("sec: company_tickers.json was not a JSON object")

        mapping: dict[str, str] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            ticker, raw_cik = entry.get("ticker"), entry.get("cik_str")
            if not ticker or raw_cik is None:
                continue
            try:
                mapping[str(ticker).upper()] = normalize_cik(raw_cik)
            except ValueError:
                continue
        return mapping

    async def company_concept(
        self, cik: str | int, tag: str, *, taxonomy: str = "us-gaap"
    ) -> dict[str, Any] | None:
        """One XBRL concept's full reported history for one filer.

        ``None`` rather than an exception when the filer has never reported the
        tag: EDGAR answers 404 for that, it is the ordinary case across a list of
        candidate tags, and it must not fail the concepts that did return.

        Deliberately the per-concept endpoint and not ``companyfacts``: the
        latter is one request but ships every tag the filer has ever used, which
        for a large issuer is tens of megabytes to parse and discard.
        """
        padded = normalize_cik(cik)
        try:
            payload = await self._http.get_json(
                f"/api/xbrl/companyconcept/CIK{padded}/{taxonomy}/{tag}.json"
            )
        except ProviderError:
            return None
        return payload if isinstance(payload, dict) else None

    async def submissions(self, cik: str | int) -> dict[str, Any]:
        padded = normalize_cik(cik)
        payload = await self._http.get_json(f"/submissions/CIK{padded}.json")
        if not isinstance(payload, dict):
            raise ProviderResponseError(f"sec: submissions for CIK{padded} was not a JSON object")
        return payload

    async def recent_for_cik(
        self,
        cik: str | int,
        *,
        forms: Sequence[str] | None = None,
        limit: int = 20,
        since: dt.date | None = None,
    ) -> list[RawSourceDocument]:
        """Recent filings for one company, newest first.

        Filtered to ``forms`` (defaulting to the priority set) so that routine
        administrative filings do not consume classifier budget.
        """
        payload = await self.submissions(cik)
        wanted = {form.upper() for form in (forms or PRIORITY_FORMS)}
        padded = normalize_cik(cik)
        company_name = payload.get("name")
        raw_tickers = payload.get("tickers")
        tickers: list[Any] = raw_tickers if isinstance(raw_tickers, list) else []

        documents: list[RawSourceDocument] = []
        for row in parse_recent_filings(payload):
            form = str(row.get("form", "")).upper()
            if wanted and form not in wanted:
                continue
            filed = _parse_date(row.get("filingDate"))
            if since is not None and filed is not None and filed.date() < since:
                continue
            documents.append(self._filing_document(row, padded, company_name, tickers, payload))
            if len(documents) >= limit:
                break

        METRICS.inc(
            "stockbrain_news_items_received_total",
            len(documents),
            labels={"provider": "sec", "mode": "company"},
        )
        return documents

    def _filing_document(
        self,
        row: dict[str, Any],
        padded_cik: str,
        company_name: Any,
        tickers: list[Any],
        submissions: dict[str, Any],
    ) -> RawSourceDocument:
        accession = str(row.get("accessionNumber", ""))
        accession_nodash = accession.replace("-", "")
        primary = row.get("primaryDocument")
        numeric_cik = padded_cik.lstrip("0") or "0"

        if accession_nodash and primary:
            url = _FILING_INDEX_URL.format(
                cik=numeric_cik, accession_nodash=accession_nodash, document=primary
            )
        else:
            url = _FILING_PAGE_URL.format(cik=padded_cik, form=row.get("form", ""))

        form = str(row.get("form", "")).upper()
        name = str(company_name) if company_name else padded_cik
        description = row.get("primaryDocDescription") or form
        # `items` lists the 8-K item numbers, which say what the filing is about.
        items = row.get("items")

        filed_at = _parse_datetime(row.get("acceptanceDateTime")) or _parse_date(
            row.get("filingDate")
        )

        # Filing titles are templated, so they must carry enough to tell two
        # filings of the same form apart in a list: the form, the date, and the
        # 8-K item numbers when present.
        filed_date = str(row.get("filingDate") or "")
        headline = f"{name} filed Form {form}"
        if items:
            headline = f"{headline} (items {items})"
        elif description and str(description).upper() not in (form, f"FORM {form}"):
            headline = f"{headline}: {description}"
        if filed_date:
            headline = f"{headline} — {filed_date}"

        body_parts = [f"Form: {form}", f"Company: {name}", f"CIK: {padded_cik}"]
        if items:
            body_parts.append(f"Items: {items}")
        if row.get("reportDate"):
            body_parts.append(f"Report date: {row['reportDate']}")
        body_parts.append(f"Accession: {accession}")

        return RawSourceDocument(
            provider=SourceProvider.SEC,
            # The accession number is EDGAR's globally unique filing identifier.
            provider_item_id=accession or None,
            url=url,
            source_name="SEC EDGAR",
            source_category=SourceCategory.REGULATOR,
            headline=headline,
            author=None,
            published_at=filed_at,
            updated_at_source=None,
            body="\n".join(body_parts),
            symbols=[str(ticker).upper() for ticker in tickers if ticker],
            # Each accession number is a distinct filing and therefore a distinct
            # event, regardless of how similar the titles are.
            is_distinct_event=True,
            raw_payload=row,
            metadata={
                "cik": padded_cik,
                "form": form,
                "accession_number": accession,
                "items": items,
                "sic": submissions.get("sic"),
                "sic_description": submissions.get("sicDescription"),
                "exchanges": submissions.get("exchanges"),
            },
        )


def _parse_date(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # EDGAR acceptance timestamps are US Eastern; without a zone we cannot
        # convert safely, so the value is rejected rather than guessed.
        return None
    return parsed.astimezone(dt.UTC)
