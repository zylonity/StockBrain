"""GlobeNewswire's per-country RSS feeds, one adapter instance per country.

``dc:identifier`` is the language-agnostic release number shared by the
English and French variants, so it is the ``release_id``.  ``dc:language``
carries the variant.  The namespace URI the feed declares is non-standard
(``http://dublincore.org/documents/dcmi-namespace/``), so fields are read by
local name rather than by a hard-coded namespace.
"""

from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["GlobeNewswireFeed"]

_BASE_URL = "https://www.globenewswire.com"
_NATIVE_LANGUAGE = "en"

#: A country's default language, used only when a release has no English
#: variant in the batch.
_COUNTRY_LANGUAGES = {
    "france": "fr",
    "netherlands": "nl",
    "belgium": "nl",
    "portugal": "pt",
    "spain": "es",
    "canada": "en",
}

#: ``(TSX: X)`` / ``(TSXV: X)`` in a title or description is a hint, not
#: identity: the resolver still treats it as rung-3 evidence.
_TICKER_RE = re.compile(r"\((TSXV?):\s*([A-Za-z0-9.\-]+)\)", re.IGNORECASE)


class GlobeNewswireFeed:
    """Parse one country's RSS feed; variants share ``dc:identifier``."""

    provider = SourceProvider.GLOBENEWSWIRE

    def __init__(
        self,
        settings: Settings,
        *,
        country: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.country = country
        self.name = f"globenewswire_{country.lower()}"
        self.native_language = _COUNTRY_LANGUAGES.get(country.lower(), _NATIVE_LANGUAGE)
        self.max_pages = 1
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=self.native_language,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(
            f"/RssFeed/country/{self.country}", language=self.native_language
        )
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        if not content or not content.strip():
            raise ProviderResponseError("globenewswire: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("globenewswire: response was not RSS XML") from exc
        if root.tag.split("}")[-1] != "rss":
            raise ProviderResponseError("globenewswire: root element was not rss")
        channel = root.find("channel")
        if channel is None:
            raise ProviderResponseError("globenewswire: no channel element (page shape changed)")
        raw_items = channel.findall("item")
        if not raw_items:
            raise ProviderResponseError("globenewswire: feed carried no items")
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("globenewswire: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        fields = {child.tag.split("}")[-1]: (child.text or "").strip() for child in element}
        release_id = fields.get("identifier", "")
        headline = fields.get("title", "")
        link = fields.get("link", "")
        if not release_id or not headline or not link:
            return None
        published_at = _parse_datetime(fields.get("pubDate"))
        if published_at is None:
            return None
        ticker_match = _TICKER_RE.search(f"{headline}\n{fields.get('description', '')}")
        subjects = [
            (child.text or "").strip()
            for child in element
            if child.tag.split("}")[-1] == "subject" and (child.text or "").strip()
        ]
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=fields.get("language") or self.native_language,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=fields.get("contributor") or None,
            ticker=ticker_match.group(2).upper() if ticker_match else None,
            exchange_hint=None,
            category=subjects[0] if subjects else self.country,
            raw={**fields, "subjects": subjects},
        )


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
