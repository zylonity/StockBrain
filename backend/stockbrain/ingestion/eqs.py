"""The EQS News homepage realtime list.

Each item is an anchor carrying ``data-news-*`` attributes and an
``…/news/<category-slug>/<headline-slug>/<uuid>_<lang>`` href.  The category is
read from the URL path rather than ``data-news-category``: the fixture's
directors-dealings attribute contains an unescaped apostrophe and parses as
``Directors``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from lxml import etree
from lxml import html as lxml_html

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["EQSFeed"]

_BASE_URL = "https://www.eqs-news.com"
_PATH = "/"
_NATIVE_LANGUAGE = "de"

#: A real ISIN, not EQS's ``noisinNNNNNN`` placeholder.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CATEGORY_RE = re.compile(r"/news/([^/]+)/")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}$")
_DATE_FORMAT = "%d %B %Y %H:%M"
_ITEM_XPATH = '//a[@data-wio="news-feed-list-item"]'
#: The page prints wall-clock Europe/Berlin time (CET/CEST), not UTC --
#: confirmed by its own ``data-current-time`` attribute sitting two hours
#: behind the displayed time of the item captured at the same moment.
_BERLIN = ZoneInfo("Europe/Berlin")


class EQSFeed:
    """Parse the 30-item realtime list; it has no pagination without JS."""

    name = "eqs"
    provider = SourceProvider.EQS
    native_language = _NATIVE_LANGUAGE

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.max_pages = 1
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(_PATH, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        if not content or not content.strip():
            raise ProviderResponseError("eqs: empty response")
        try:
            root = lxml_html.fromstring(content)
        except (etree.ParserError, etree.XMLSyntaxError) as exc:
            raise ProviderResponseError("eqs: response was not HTML") from exc

        anchors = root.xpath(_ITEM_XPATH)
        if not anchors:
            raise ProviderResponseError("eqs: no news items found (page shape changed)")
        default_date = _first_date(root)
        items = [
            item
            for anchor in anchors
            if (item := self._parse_item(anchor, default_date)) is not None
        ]
        if not items:
            raise ProviderResponseError("eqs: news items carried no usable releases")
        return items

    def _parse_item(self, anchor: Any, default_date: str | None) -> FeedItem | None:
        href = (anchor.get("href") or "").strip()
        release_id = (anchor.get("data-news-item") or "").strip()
        if not href or not release_id:
            return None
        language = _language_from_url(href) or _language_from_attribute(anchor) or _NATIVE_LANGUAGE
        category = _category_from_url(href)
        company = _first_text(anchor, './/h4[contains(@class,"news__company")]')
        heading = _first_text(anchor, './/p[contains(@class,"news__heading")]')
        headline = heading or company
        if not headline:
            return None
        date_text = _preceding_date(anchor) or default_date
        time_text = _first_text(anchor, './/span[contains(@class,"news__time")]')
        published_at = _parse_datetime(date_text, time_text)
        if published_at is None:
            return None
        raw_isin = (anchor.get("data-news-isin") or "").strip()
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=language,
            url=href,
            headline=headline,
            published_at=published_at,
            company_name=company or None,
            isin=raw_isin if _ISIN_RE.match(raw_isin) else None,
            exchange_hint=None,
            category=category,
            raw={
                "data-news-item": release_id,
                "data-news-uuid": anchor.get("data-news-uuid"),
                "data-news-languages": anchor.get("data-news-languages"),
                "data-news-isin": raw_isin or None,
                "data-news-category": anchor.get("data-news-category"),
                "href": href,
                "company": company,
                "heading": heading,
                "date": date_text,
                "time": time_text,
            },
        )


def _first_text(root: Any, xpath: str) -> str:
    matches = root.xpath(xpath)
    if not matches:
        return ""
    return " ".join((matches[0].text_content() or "").split())


def _first_date(root: Any) -> str | None:
    for element in root.xpath('//p[contains(@class,"news__date")]'):
        text = " ".join((element.text_content() or "").split())
        if text:
            return text
    return None


def _preceding_date(anchor: Any) -> str | None:
    matches = anchor.xpath('preceding-sibling::p[contains(@class,"news__date")][1]')
    if not matches:
        return None
    return " ".join((matches[0].text_content() or "").split()) or None


def _category_from_url(href: str) -> str | None:
    match = _CATEGORY_RE.search(urlsplit(href).path)
    return match.group(1).lower() if match else None


def _language_from_url(href: str) -> str | None:
    segment = urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
    suffix = segment.rsplit("_", 1)[-1].lower()
    return suffix if _LANGUAGE_RE.match(suffix) else None


def _language_from_attribute(anchor: Any) -> str | None:
    raw = anchor.get("data-news-languages")
    if not raw:
        return None
    try:
        languages = json.loads(raw)
    except ValueError:
        return None
    if isinstance(languages, dict):
        values = list(languages.values())
    elif isinstance(languages, list):
        values = languages
    else:
        return None
    return values[0] if values else None


def _parse_datetime(date_text: str | None, time_text: str | None) -> dt.datetime | None:
    """Parse the page's Europe/Berlin wall-clock time and convert it to UTC."""
    if not date_text or not time_text:
        return None
    try:
        local = dt.datetime.strptime(f"{date_text} {time_text}", _DATE_FORMAT).replace(
            tzinfo=_BERLIN
        )
    except ValueError:
        return None
    return local.astimezone(dt.UTC)
