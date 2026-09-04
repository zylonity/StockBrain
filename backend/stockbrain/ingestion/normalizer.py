"""Normalisation of raw source documents.

Three jobs, all deterministic and all testable without a network:

* **URL canonicalisation** -- so that the same article arriving with different
  tracking parameters, casing or a trailing slash collapses to one identity.
* **HTML to text** -- provider bodies may contain markup.  The extracted text is
  what gets hashed and what an LLM sees; the raw body is kept separately for
  audit and is never rendered without sanitisation.
* **Content hashing** -- SHA-256 over the normalised headline plus body, which
  catches verbatim syndication that a URL comparison misses.

Nothing here interprets the content.  Everything it produces is data.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from stockbrain.enums import SourceCategory
from stockbrain.ingestion.base import RawSourceDocument

__all__ = [
    "TRACKING_PARAMS",
    "TRACKING_PARAM_PREFIXES",
    "NormalizedSource",
    "canonicalize_url",
    "classify_source_category",
    "content_hash",
    "html_to_text",
    "normalize_document",
    "normalize_whitespace",
]

#: Query parameters that identify a referral, not a document.  Removing them is
#: what makes the same article shared from three places one source.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "cmpid",
        "ega",
        "fbclid",
        "gclid",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
        "icid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "ncid",
        "partner",
        "ref",
        "referrer",
        "s_cid",
        "sh",
        "source",
        "sref",
        "trk",
        "twclid",
        "vero_conv",
        "vero_id",
        "yclid",
        "_ga",
        "_gl",
        "__twitter_impression",
    }
)

TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "at_", "ito_", "cmp_", "spm_")

_DEFAULT_PORTS = {"http": "80", "https": "443"}

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_BLOCK_BREAK_RE = re.compile(
    r"</(p|div|section|article|li|tr|h[1-6]|blockquote)\s*>|<br\s*/?>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
# Includes NO-BREAK SPACE (U+00A0) and ZERO WIDTH SPACE (U+200B): scraped
# articles are full of both, and leaving them in would defeat content hashing.
_WHITESPACE_RE = re.compile("[ \\t\\u00a0\\u200b]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

#: Transparent, editable trust categories.  Matched on the registrable-ish host
#: suffix so subdomains inherit the category.
_CATEGORY_BY_HOST_SUFFIX: tuple[tuple[str, SourceCategory], ...] = (
    ("sec.gov", SourceCategory.REGULATOR),
    ("federalreserve.gov", SourceCategory.REGULATOR),
    ("fca.org.uk", SourceCategory.REGULATOR),
    ("esma.europa.eu", SourceCategory.REGULATOR),
    ("fda.gov", SourceCategory.REGULATOR),
    ("ftc.gov", SourceCategory.REGULATOR),
    ("londonstockexchange.com", SourceCategory.REGULATOR),
    ("nasdaq.com", SourceCategory.REGULATOR),
    ("nyse.com", SourceCategory.REGULATOR),
    (".gov", SourceCategory.GOVERNMENT),
    (".gov.uk", SourceCategory.GOVERNMENT),
    ("europa.eu", SourceCategory.GOVERNMENT),
    ("benzinga.com", SourceCategory.NEWSWIRE),
    ("businesswire.com", SourceCategory.NEWSWIRE),
    ("prnewswire.com", SourceCategory.NEWSWIRE),
    ("globenewswire.com", SourceCategory.NEWSWIRE),
    ("reuters.com", SourceCategory.NEWSWIRE),
    ("apnews.com", SourceCategory.NEWSWIRE),
    ("bloomberg.com", SourceCategory.PRESS),
    ("ft.com", SourceCategory.PRESS),
    ("wsj.com", SourceCategory.PRESS),
    ("cnbc.com", SourceCategory.PRESS),
    ("bbc.co.uk", SourceCategory.PRESS),
    ("bbc.com", SourceCategory.PRESS),
    ("theguardian.com", SourceCategory.PRESS),
    ("nytimes.com", SourceCategory.PRESS),
    ("barrons.com", SourceCategory.PRESS),
    ("marketwatch.com", SourceCategory.PRESS),
    ("investors.com", SourceCategory.PRESS),
)


class NormalizedSource(BaseModel):
    """A raw document plus the deterministic fields dedupe and storage need."""

    model_config = ConfigDict(extra="forbid")

    document: RawSourceDocument
    canonical_url: str | None
    normalized_text: str
    content_hash: str
    normalized_headline: str
    source_category: SourceCategory


def canonicalize_url(url: str | None) -> str | None:
    """Reduce a URL to a stable identity.

    Lowercases scheme and host, drops a default port, removes ``www.``, strips
    the fragment and known tracking parameters, sorts the remaining query, and
    normalises the trailing slash.  Returns ``None`` for input that is not an
    absolute http(s) URL, because a relative or malformed URL is not an identity.
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None

    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(kept))

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def html_to_text(value: str | None) -> str:
    """Extract readable text from a possibly-HTML body.

    Script and style blocks are removed entirely -- their contents are never
    part of an article and must never reach an LLM or the hash.
    """
    if not value:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", value)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def normalize_whitespace(value: str | None) -> str:
    """Collapse a string to a comparable form: NFKC, folded case, single spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _WHITESPACE_RE.sub(" ", text.replace("\n", " "))
    return text.strip().casefold()


def content_hash(headline: str | None, body: str | None) -> str:
    """SHA-256 over the normalised headline and body.

    Two syndications of the same wire story hash identically even when their
    URLs differ, which is the whole point of this layer.
    """
    digest = hashlib.sha256()
    digest.update(normalize_whitespace(headline).encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(normalize_whitespace(body).encode("utf-8"))
    return digest.hexdigest()


def classify_source_category(url: str | None, source_name: str | None = None) -> SourceCategory:
    """Assign a transparent trust category from the host.

    Deliberately a lookup table rather than a model judgement: the categories
    must be inspectable and editable, and must not drift between runs.
    """
    host = ""
    if url:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
    if host.startswith("www."):
        host = host[4:]

    if host:
        for suffix, category in _CATEGORY_BY_HOST_SUFFIX:
            if host == suffix.lstrip(".") or host.endswith(
                suffix if suffix.startswith(".") else f".{suffix}"
            ):
                return category

    if source_name:
        lowered = source_name.strip().lower()
        for suffix, category in _CATEGORY_BY_HOST_SUFFIX:
            name = suffix.lstrip(".").split(".")[0]
            if name and name in lowered:
                return category
    return SourceCategory.UNKNOWN


def normalize_document(document: RawSourceDocument) -> NormalizedSource:
    """Compute every deterministic field the storage and dedupe layers need."""
    canonical = canonicalize_url(document.url)
    text = html_to_text(document.body)
    category = (
        document.source_category
        if document.source_category is not SourceCategory.UNKNOWN
        else classify_source_category(document.url, document.source_name)
    )
    return NormalizedSource(
        document=document,
        canonical_url=canonical,
        normalized_text=text,
        content_hash=content_hash(document.headline, text),
        normalized_headline=normalize_whitespace(document.headline),
        source_category=category,
    )
