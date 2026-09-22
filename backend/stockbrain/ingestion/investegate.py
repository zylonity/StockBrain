"""The Investegate front-page RNS list.

Rows are ``<tr>`` with a datetime cell, a source-code cell, a company cell and
an announcement link whose last path segment is the language-agnostic release
id.  The announcement page itself is never fetched here: the body is the
existing ``CONTENT_EXTRACT`` path's job, on demand.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from lxml import etree
from lxml import html as lxml_html

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["InvestegateFeed"]

_BASE_URL = "https://www.investegate.co.uk"
_NATIVE_LANGUAGE = "en"
_MAIN_EXCHANGE = "London Stock Exchange"

_DATETIME_FORMAT = "%d %b %Y %I:%M %p"
#: The page prints wall-clock Europe/London time (GMT/BST), not UTC.
_LONDON = ZoneInfo("Europe/London")
_COMPANY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<ticker>[A-Za-z0-9.\-]+)\)\s*$")
_ANNOUNCEMENT_LINK = 'a[contains(concat(" ", normalize-space(@class), " "), " announcement-link ")]'


class InvestegateFeed:
    """Parse the all-market RNS list; the body is the extractor's job."""

    name = "investegate"
    provider = SourceProvider.INVESTEGATE
    native_language = _NATIVE_LANGUAGE

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.max_pages = settings.investegate_max_pages
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        path = "/" if page <= 1 else f"/?page={page}"
        content = await self._http.get(path, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        """Parse one front-page table.  Raises when the table is gone."""
        if not content or not content.strip():
            raise ProviderResponseError("investegate: empty response")
        try:
            root = lxml_html.fromstring(content)
        except (etree.ParserError, etree.XMLSyntaxError) as exc:
            raise ProviderResponseError("investegate: response was not HTML") from exc

        rows = root.xpath(f"//{_ANNOUNCEMENT_LINK}/ancestor::tr[1]")
        if not rows:
            raise ProviderResponseError(
                "investegate: no announcement rows found (page shape changed)"
            )
        items = [item for row in rows if (item := self._parse_row(row)) is not None]
        if not items:
            raise ProviderResponseError("investegate: rows carried no usable announcements")
        return items

    def _parse_row(self, row: Any) -> FeedItem | None:
        cells = row.xpath("./td")
        if len(cells) < 4:
            return None
        published_at = _parse_datetime(_text(cells[0]))
        links = cells[3].xpath(f".//{_ANNOUNCEMENT_LINK}")
        if not links:
            return None
        href = (links[0].get("href") or "").strip()
        headline = _text(links[0])
        release_id = href.rstrip("/").rsplit("/", 1)[-1]
        source_code = _text(cells[1])
        company = _text(cells[2])
        name, ticker = _split_company(company)
        if not published_at or not href or not headline or not release_id or not source_code:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=href,
            headline=headline,
            published_at=published_at,
            company_name=name,
            ticker=ticker,
            exchange_hint=_MAIN_EXCHANGE,
            category=source_code,
            raw={
                "datetime": _text(cells[0]),
                "source": source_code,
                "company": company,
                "headline": headline,
                "href": href,
            },
        )


def _text(element: Any) -> str:
    return " ".join((element.text_content() or "").split())


def _split_company(company: str) -> tuple[str | None, str | None]:
    match = _COMPANY_RE.match(company)
    if match is None:
        return (company or None, None)
    name = match.group("name").strip()
    return (name or None, match.group("ticker").upper())


def _parse_datetime(value: str) -> dt.datetime | None:
    """Parse the page's Europe/London wall-clock time and convert it to UTC."""
    try:
        local = dt.datetime.strptime(value.strip(), _DATETIME_FORMAT).replace(tzinfo=_LONDON)
    except ValueError:
        return None
    return local.astimezone(dt.UTC)
