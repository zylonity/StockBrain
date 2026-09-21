"""The two CNMV RSS feeds: inside information and other relevant information.

The feed is a non-standard RSS document: a capitalised ``Channel`` element, a
capitalised ``Title`` per item, and a description of ``<b>time date</b>
category<BR/><BR/>detail``. The headline is the category plus the issuer detail
that follows it -- the classifier's only readable text for this provider --
with just the bold timestamp dropped. An empty channel is a valid answer for
inside information; a missing channel is a shape change and raises.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["CNMVFeed"]

_BASE_URL = "https://www.cnmv.es"
_NATIVE_LANGUAGE = "es"
_EXCHANGE_HINT = "Bolsa de Madrid"

#: The description is ``<b>time date</b> category<BR/><BR/>detail``.  Only the
#: bold timestamp is discarded; the category and the detail after it are the
#: classifier's only readable text for this provider.
_BOLD_STAMP_RE = re.compile(r"<b>.*?</b>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

#: kind -> (path, feed label, allow_empty)
_KINDS: dict[str, tuple[str, str, bool]] = {
    "ip": (
        "/Portal/Informacion-privilegiada/RSS.asmx/GetNoticiasCNMV",
        "Información privilegiada",
        True,
    ),
    "oir": (
        "/Portal/Otra-Informacion-Relevante/RSS.asmx/GetNoticiasCNMV",
        "Otra información relevante",
        False,
    ),
}


class CNMVFeed:
    """Parse one CNMV feed.  The feed is Spanish only; there are no variants."""

    provider = SourceProvider.CNMV
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self,
        settings: Settings,
        *,
        kind: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError(f"CNMVFeed kind must be one of {sorted(_KINDS)}, got {kind!r}")
        path, category, allow_empty = _KINDS[kind]
        self.kind = kind
        self.name = f"cnmv_{kind}"
        self.category = category
        self.allow_empty = allow_empty
        self.max_pages = 1
        self._path = path
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(self._path, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        """Parse one feed.

        A missing ``Channel`` is a shape change and raises.  A present channel
        with no items is a *valid* empty feed -- the caller's ``allow_empty``
        decides whether that is expected for this feed.
        """
        if not content or not content.strip():
            raise ProviderResponseError("cnmv: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("cnmv: response was not XML") from exc

        channel = root.find("Channel")
        if channel is None:
            raise ProviderResponseError("cnmv: no Channel element (page shape changed)")
        raw_items = channel.findall("item")
        if not raw_items:
            return []
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("cnmv: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        link = _clean(element.findtext("link"))
        title = _clean(element.findtext("Title"))
        description = _clean(element.findtext("description"))
        published_at = _parse_datetime(element.findtext("pubDate"))
        release_id = _release_id(link)
        headline = _headline(description)
        if not link or not headline or not release_id or published_at is None:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=title or None,
            exchange_hint=_EXCHANGE_HINT,
            category=self.category,
            raw={
                "Title": title,
                "link": link,
                "pubDate": element.findtext("pubDate"),
                "description": description,
            },
        )


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _release_id(link: str) -> str | None:
    if not link:
        return None
    query = parse_qs(urlparse(link).query)
    values = query.get("nreg")
    return values[0] if values else None


def _headline(description: str) -> str:
    """The category label plus the issuer detail that follows it.

    Only the bold ``time date`` stamp is dropped; the rest of the description
    -- category and detail alike -- survives, because for CNMV this is the
    only readable text the classifier ever sees (bodies are ``None``).
    """
    if not description:
        return ""
    text = _BOLD_STAMP_RE.sub(" ", description, count=1)
    text = _BR_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _clean(html.unescape(text))


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
