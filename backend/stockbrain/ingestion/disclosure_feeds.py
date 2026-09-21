"""Shared vocabulary for the keyless non-US disclosure feeds.

One release from one wire becomes one :class:`FeedItem`; the handler filters
boilerplate, groups language variants, and maps the survivors onto the ordinary
:class:`~stockbrain.ingestion.base.RawSourceDocument` path.  Nothing here
interprets content.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import ProviderRateLimited, ProviderUnavailable
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.ingestion.base import RawSourceDocument

__all__ = [
    "BoilerplateRule",
    "DisclosureFeed",
    "FeedHttpClient",
    "FeedItem",
    "group_releases",
    "is_boilerplate",
    "to_document",
]

#: A boilerplate rule: a compiled pattern and the human-readable rule id it
#: came from.  Data rather than code so the table can grow without a logic
#: review.
BoilerplateRule = tuple[re.Pattern[str], str]

_CNMV_IP_LABEL = "Información privilegiada"
_CNMV_OIR_LABEL = "Otra información relevante"

#: Typographic apostrophes are normalised before matching so a rule written
#: with an ASCII apostrophe still matches a feed that prints U+2019.
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'"})

_BOILERPLATE_RULES: dict[SourceProvider, tuple[BoilerplateRule, ...]] = {
    SourceProvider.INVESTEGATE: tuple(
        (re.compile(pattern, re.IGNORECASE), pattern)
        for pattern in (
            r"Transaction in Own Shares",
            r"Holding\(s\) in Company",
            r"Director/PDMR Shareholding",
            r"\bForm 8\b",
            r"\bForm 38\.5",
            r"Total Voting Rights",
            r"Exercise of Warrants",
            r"Block Listing",
            r"Investor Presentation via Investor Meet Company",
        )
    ),
    SourceProvider.EQS: (
        (re.compile(r"voting-rights", re.IGNORECASE), "EQS category voting-rights"),
        (re.compile(r"directors-dealings", re.IGNORECASE), "EQS category directors-dealings"),
        (
            re.compile(r"Release of a capital market information", re.IGNORECASE),
            "Art. 5 capital market information",
        ),
        (re.compile(r"Transaction in Own Shares", re.IGNORECASE), "Transaction in Own Shares"),
    ),
    SourceProvider.CNMV: (
        (
            re.compile(r"Programas de recompra de acciones", re.IGNORECASE),
            "Programas de recompra de acciones",
        ),
        (re.compile(r"Contratos de liquidez", re.IGNORECASE), "Contratos de liquidez"),
        (
            re.compile(
                r"Sobre suspensiones.*(?:SOCIETE GENERALE EFFEKTEN"
                r"|BNP PARIBAS ISSUANCE|CITIGROUP GLOBAL MARKETS)",
                re.IGNORECASE | re.DOTALL,
            ),
            "Sobre suspensiones for warrant issuers",
        ),
    ),
    SourceProvider.GLOBENEWSWIRE: tuple(
        (re.compile(pattern, re.IGNORECASE), pattern)
        for pattern in (
            r"Déclaration des opérations de rachat",
            r"Disclosure of trading in own shares",
            r"Déclaration hebdomadaire des transactions",
            r"Number of outstanding shares and voting rights",
            # The fixture prints "NOMBRE TOTAL D'ACTIONS ET DE DROITS DE VOTE";
            # the optional "total" matches the real wording without widening
            # the rule to unrelated notices.
            r"Nombre (?:total )?d'actions et de droits de vote",
        )
    ),
}

_SOURCE_NAMES: dict[SourceProvider, str] = {
    SourceProvider.INVESTEGATE: "Investegate",
    SourceProvider.EQS: "EQS News",
    SourceProvider.CNMV: "CNMV",
    SourceProvider.GLOBENEWSWIRE: "GlobeNewswire",
    SourceProvider.ACTUSNEWS: "Actusnews Wire",
}

_EQS_REGULATORY_CATEGORIES = frozenset(
    {
        "voting-rights",
        "directors-dealings",
        "other-capital-market-information",
        "uk-regulatory",
        "ad-hoc",
    }
)


@dataclass(slots=True)
class FeedItem:
    """One release as a feed printed it, before it becomes a source row."""

    provider: SourceProvider
    release_id: str
    language: str
    url: str
    headline: str
    published_at: dt.datetime
    company_name: str | None = None
    ticker: str | None = None
    isin: str | None = None
    exchange_hint: str | None = None
    category: str | None = None
    alternate_language_urls: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DisclosureFeed(Protocol):
    """One pollable disclosure list."""

    name: str
    provider: SourceProvider
    native_language: str
    max_pages: int
    allow_empty: bool

    async def fetch_page(self, page: int) -> list[FeedItem]: ...

    async def aclose(self) -> None: ...


class FeedHttpClient(ProviderHttpClient):
    """The shared fetch helper: plain text, one attempt, classified errors.

    ``ProviderHttpClient`` decodes JSON and classifies 429 as a rate-limit
    error.  A disclosure feed answers HTML or XML, and the whole poll is the
    unit that must stop on a 429 -- so text is returned and 429 is mapped to
    :class:`ProviderUnavailable`, which the handler records as ``DOWN``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str,
        base_url: str,
        language: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        contact = (
            settings.disclosure_feed_user_agent_contact.strip()
            or settings.sec_contact_email.strip()
            or "unset"
        )
        # Kept on ``self`` and re-sent on every request in :meth:`get` below,
        # rather than relied on as client-level defaults: ``ProviderHttpClient``
        # only applies its ``headers=`` kwarg when it builds its *own*
        # ``httpx.AsyncClient``.  An injected client -- every test's only way to
        # control the transport -- silently drops them otherwise.
        self._default_headers = {
            "User-Agent": f"{settings.app_name}/{settings.app_version} (+{contact})",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/rss+xml;q=0.8,text/xml;q=0.8,*/*;q=0.5"
            ),
            "Accept-Language": language,
        }
        super().__init__(
            provider=provider,
            base_url=base_url,
            headers=self._default_headers,
            timeout_seconds=settings.disclosure_feed_timeout_seconds,
            # ``client`` is passed through so a test can inject a mock transport
            # and own its lifecycle, exactly as ``ProviderHttpClient`` allows.
            client=client,
        )

    async def get(self, url: str, *, language: str) -> str:
        """GET one feed URL with no retry and the request's language preference."""
        headers = dict(self._default_headers)
        headers["Accept-Language"] = language
        # ``request_json`` is typed ``Any`` (it decodes JSON); this subclass
        # decodes text, so the result is narrowed through a typed local rather
        # than leaked out of a ``-> str`` method as ``Any``.
        text: str = await self.request_json(
            "GET",
            url,
            headers=headers,
            retry_safe=False,
        )
        return text

    def _decode(self, response: httpx.Response) -> str:
        return response.text

    def _classify(self, response: httpx.Response) -> Exception:
        error = super()._classify(response)
        if isinstance(error, ProviderRateLimited):
            return ProviderUnavailable(f"{self.provider}: rate limited (HTTP 429)")
        return error


def _source_category(item: FeedItem) -> SourceCategory:
    """The transparent trust category a feed item maps onto."""
    if item.provider is SourceProvider.CNMV:
        return SourceCategory.REGULATOR
    if item.provider is SourceProvider.INVESTEGATE:
        return SourceCategory.REGULATOR if item.category == "RNS" else SourceCategory.ISSUER
    if item.provider is SourceProvider.EQS:
        if (item.category or "").lower() in _EQS_REGULATORY_CATEGORIES:
            return SourceCategory.REGULATOR
        return SourceCategory.ISSUER
    if item.provider in (SourceProvider.GLOBENEWSWIRE, SourceProvider.ACTUSNEWS):
        return SourceCategory.ISSUER
    return SourceCategory.UNKNOWN


def _exchange_hints(item: FeedItem) -> list[str]:
    hints: list[str] = []
    if item.exchange_hint:
        hints.append(item.exchange_hint)
    if item.provider is SourceProvider.INVESTEGATE:
        # The page does not say LSE or AIM, so both travel as alternates.
        aim = "London Stock Exchange AIM"
        if aim not in hints:
            hints.append(aim)
    return hints


def _canonical_headline(item: FeedItem) -> str:
    """Prefix the issuer name onto a headline that does not already carry it.

    Bodies are never fetched for these feeds (``body=None`` below), so the
    headline is the classifier's only readable text. A generic category label
    -- EQS's "Release of a capital market information", say -- names no
    company, and without a ticker there is nothing else for the model to
    resolve an instrument from. The raw, unprefixed headline is unaffected:
    it is still reachable through the item's own fields and ``raw_payload``.
    """
    headline = item.headline
    company = item.company_name
    if not company or company.casefold() in headline.casefold():
        return headline
    return f"{company}: {headline}"


def to_document(item: FeedItem) -> RawSourceDocument:
    """Map one feed item onto the canonical ingestion DTO.

    Validation is real rather than nominal: ``RawSourceDocument`` accepts an
    empty or relative URL, an empty headline, or a naive timestamp, so a
    malformed row is refused here instead of becoming an untitled,
    unreachable, or wrongly-timed source.
    """
    if not item.release_id or not item.release_id.strip():
        raise ValueError("release_id is empty")
    if not item.headline or not item.headline.strip():
        raise ValueError("headline is empty")
    parsed_url = urlsplit(item.url or "")
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError(f"url is not an absolute http(s) URL: {item.url!r}")
    if item.published_at.tzinfo is None:
        raise ValueError("published_at is naive, not timezone-aware")
    published_at = item.published_at.astimezone(dt.UTC)

    category = _source_category(item)
    return RawSourceDocument(
        provider=item.provider,
        provider_item_id=item.release_id,
        url=item.url,
        source_name=_SOURCE_NAMES.get(item.provider, item.provider.value.title()),
        source_category=category,
        headline=_canonical_headline(item),
        published_at=published_at,
        # Bodies are the extractor's job, on demand and budgeted.
        body=None,
        symbols=[item.ticker] if item.ticker else [],
        is_distinct_event=category is SourceCategory.REGULATOR,
        raw_payload=dict(item.raw),
        metadata={
            "language": item.language,
            "isin": item.isin,
            "exchange_hint": item.exchange_hint,
            "exchange_hints": _exchange_hints(item),
            "alternate_language_urls": dict(item.alternate_language_urls),
            "feed_category": item.category,
            "company_name": item.company_name,
        },
    )


def is_boilerplate(item: FeedItem) -> bool:
    """Whether a feed item is routine noise that must never be stored.

    Matched against the headline, the feed's own category label and the printed
    company name, so a rule may combine a category with an issuer (the CNMV
    warrant-suspension case).  The CNMV inside-information feed is never
    filtered: a suspension it carries is a real event.
    """
    rules = _BOILERPLATE_RULES.get(item.provider)
    if not rules:
        return False
    if item.provider is SourceProvider.CNMV and item.category == _CNMV_IP_LABEL:
        return False
    haystack = " \n ".join(
        part for part in (item.headline, item.category, item.company_name) if part
    ).translate(_APOSTROPHES)
    return any(pattern.search(haystack) for pattern, _ in rules)


def group_releases(items: Sequence[FeedItem], native_language: str = "en") -> list[FeedItem]:
    """Collapse one release's language variants into a single item.

    Within a poll batch the same ``(provider, release_id)`` may arrive in
    several languages.  English is preferred; otherwise the feed's native
    language; otherwise the first variant seen.  The others become
    ``alternate_language_urls`` on the chosen item, so translation collapse
    happens before ingestion rather than as a unique-index violation.
    """
    grouped: dict[tuple[SourceProvider, str], list[FeedItem]] = {}
    order: list[tuple[SourceProvider, str]] = []
    for item in items:
        key = (item.provider, item.release_id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    result: list[FeedItem] = []
    for key in order:
        variants = grouped[key]
        chosen = _preferred_variant(variants, native_language)
        # ``chosen`` is a reference into the caller's own list; mutating it in
        # place would corrupt whatever else holds that item and would break a
        # second call over the same batch. Build a new mapping instead: start
        # from whatever the item already carried, add each sibling variant
        # (existing entries win on a language collision), and never record a
        # same-language variant as an "alternate" of itself.
        alternates = dict(chosen.alternate_language_urls)
        for other in variants:
            if other is chosen or not other.language or other.language == chosen.language:
                continue
            alternates.setdefault(other.language, other.url)
        result.append(replace(chosen, alternate_language_urls=alternates))
    return result


def _preferred_variant(variants: Sequence[FeedItem], native_language: str) -> FeedItem:
    for item in variants:
        if item.language == "en":
            return item
    for item in variants:
        if item.language == native_language:
            return item
    return variants[0]
