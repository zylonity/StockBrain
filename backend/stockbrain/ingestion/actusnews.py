"""The Actusnews Wire English RSS feed for French issuers.

Only ``/en/rss`` is subscribed; the French feed is not.  The title convention is
``COMPANY : headline`` (also ``COMPANY - headline``), and ``dc:creator`` names
the company, so the headline is the remainder once that prefix is removed.  A
title without the separator is kept whole.
"""

from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["ActusNewsFeed"]

_BASE_URL = "https://www.actusnews.com"
_PATH = "/en/rss"
_NATIVE_LANGUAGE = "en"
_COMPANY = "Actusnews"
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class ActusNewsFeed:
    """Parse the English feed; the ``COMPANY : headline`` title is split."""

    name = "actusnews"
    provider = SourceProvider.ACTUSNEWS
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ) -> None:
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
            raise ProviderResponseError("actusnews: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("actusnews: response was not RSS XML") from exc
        if root.tag.split("}")[-1] != "rss":
            raise ProviderResponseError("actusnews: root element was not rss")
        channel = root.find("channel")
        if channel is None:
            raise ProviderResponseError(
                "actusnews: no channel element (page shape changed)"
            )
        raw_items = channel.findall("item")
        if not raw_items:
            raise ProviderResponseError("actusnews: feed carried no items")
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("actusnews: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        title = _clean(element.findtext("title"))
        link = _clean(element.findtext("link"))
        creator = _clean(element.findtext(_DC_CREATOR))
        published_at = _parse_datetime(element.findtext("pubDate"))
        release_id = link.split("/pr/", 1)[-1] if "/pr/" in link else ""
        headline = _strip_company_prefix(title, creator)
        if not release_id or not headline or not link or published_at is None:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=creator or None,
            exchange_hint=None,
            category=_COMPANY,
            raw={
                "title": title,
                "link": link,
                "guid": _clean(element.findtext("guid")),
                "creator": creator,
                "pubDate": element.findtext("pubDate"),
            },
        )


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _strip_company_prefix(title: str, creator: str) -> str:
    if creator:
        for separator in (" : ", " - "):
            prefix = f"{creator}{separator}"
            if title.startswith(prefix):
                return title[len(prefix) :].strip()
    return title


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
