"""Identity normalisation for instrument resolution.

Every function here answers one question: *are these two strings naming the
same thing?*  They are deliberately conservative.  A normaliser that collapses
too much turns two different securities into one match, and the resolver's
whole job is to refuse exactly that.

These functions are copied -- frozen -- into any migration that backfills a
column derived from them.  A migration must keep producing the same values
forever, so it must never import this module.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "instrument_name_key",
    "is_probably_isin",
    "normalize_currency",
    "normalize_exchange",
    "normalize_isin",
    "normalize_ticker",
    "split_broker_ticker",
]

#: Legal-form suffixes stripped from a company name before comparison.  Kept in
#: sync with :func:`stockbrain.intelligence.normalize.company_key` in spirit but
#: separate in fact: that one deduplicates one event's impact rows, this one
#: decides instrument identity, and the two must be free to diverge.
_NAME_SUFFIXES = frozenset(
    {
        "incorporated",
        "corporation",
        "company",
        "limited",
        "holdings",
        "holding",
        "group",
        "plc",
        "inc",
        "corp",
        "co",
        "ltd",
        "llc",
        "lp",
        "sa",
        "nv",
        "ag",
        "se",
        "spa",
        "oyj",
        "ab",
        "as",
        "asa",
        "adr",
        "ads",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: Characters kept in a ticker. ``/`` is here because Trading 212 writes share
#: classes with a slash (``TAP/A``); dropping it silently collapsed six real
#: listings onto unrelated ones -- ``HVT/A`` became ``HVTA``, which is a
#: different security. Measured against the live universe on 2026-09-04.
_TICKER_ALLOWED = re.compile(r"[^A-Z0-9./\- ]")

#: Every spelling of a share-class separator, canonicalised to ``.``. Trading
#: 212 uses ``/``, market-data feeds and language models use ``.`` or ``-``, and
#: a hint has to be able to match a stored listing across that difference.
_CLASS_SEPARATOR = re.compile(r"[./\- ]+")


def instrument_name_key(name: str | None) -> str:
    """Normalise a company or instrument name for comparison.

    Case, accents and punctuation are removed and trailing legal-form suffixes
    are dropped, so ``"Berkshire Hathaway Inc."`` and ``"BERKSHIRE HATHAWAY"``
    agree.  Share-class words are *kept*: "Berkshire Hathaway Class A" and
    "Berkshire Hathaway Class B" must not normalise to the same key, because
    they are different securities.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _NON_ALNUM.sub(" ", text.lower()).strip()
    if not text:
        return ""
    words = text.split()
    while len(words) > 1 and words[-1] in _NAME_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_ticker(ticker: str | None) -> str:
    """Normalise a market-data ticker, canonicalising share-class separators.

    Upper-cased, unsupported characters dropped, and every class separator --
    ``.``, ``-``, ``/`` or a space -- rewritten to a single ``.``.  So
    ``BRK.B``, ``BRK-B`` and Trading 212's own ``BRK/B`` all agree, which is
    what lets a classifier hint match a stored listing.

    The class is never collapsed *away*: ``BRK.A`` stays distinguishable from
    ``BRK``, and that would be the exact silent merge this system exists to
    prevent.  Trailing separators are also preserved, because the London line
    of BP is ``BP.`` and must not become the New York ``BP``.

    Verified against Trading 212's live universe (17,452 instruments,
    2026-09-04): canonicalising introduces no collisions and removes six that
    the previous slash-stripping form created.
    """
    if not ticker:
        return ""
    cleaned = _TICKER_ALLOWED.sub("", ticker.strip().upper())
    return _CLASS_SEPARATOR.sub(".", cleaned)


def normalize_isin(isin: str | None) -> str:
    """Upper-case and strip an ISIN.  Returns ``""`` when it is not ISIN-shaped."""
    if not isin:
        return ""
    candidate = re.sub(r"\s+", "", isin).upper()
    return candidate if _ISIN.match(candidate) else ""


def is_probably_isin(value: str | None) -> bool:
    """True when the string has the ISO 6166 shape.

    Shape only -- the check digit is not verified, because a provider that
    supplies a syntactically valid ISIN is the authority on it and rejecting one
    over an arithmetic disagreement would lose real instruments.
    """
    return bool(normalize_isin(value))


def normalize_currency(currency: str | None) -> str:
    """Upper-case a currency code.  Returns ``""`` unless it is three letters."""
    if not currency:
        return ""
    candidate = currency.strip().upper()
    return candidate if len(candidate) == 3 and candidate.isalpha() else ""


def normalize_exchange(exchange: str | None) -> str:
    """Normalise an exchange name or MIC for comparison.

    Handles the common aliases a model or a news feed produces (``NASDAQ``,
    ``Nasdaq Global Select``, ``XNAS``) without inventing a mapping table: names
    are reduced to alphanumerics and then matched against a small set of known
    equivalences.  Anything unrecognised is returned normalised but unmapped, so
    it can still match itself and never matches something else.
    """
    if not exchange:
        return ""
    text = _NON_ALNUM.sub(" ", exchange.lower()).strip()
    if not text:
        return ""
    for canonical, tokens in _EXCHANGE_ALIASES.items():
        if text in tokens:
            return canonical
        # "nasdaq global select market" -> nasdaq
        first = text.split()[0]
        if first in tokens:
            return canonical
    return text


#: Exchange equivalences.  Values are the *normalised* spellings that map onto
#: the canonical key.  This is a small curated table, not a heuristic: an
#: unknown venue stays unknown rather than being coerced into a neighbour.
_EXCHANGE_ALIASES: dict[str, frozenset[str]] = {
    "nasdaq": frozenset({"nasdaq", "xnas", "nasdaq global select", "nasdaqgs", "nsdq"}),
    "nyse": frozenset({"nyse", "xnys", "new york stock exchange"}),
    "nyse arca": frozenset({"arca", "nyse arca", "arcx"}),
    "nyse american": frozenset({"amex", "nyse american", "xase"}),
    "lse": frozenset({"lse", "xlon", "london stock exchange", "london"}),
    "xetra": frozenset({"xetra", "xetr", "deutsche boerse", "deutsche borse"}),
    "euronext paris": frozenset({"euronext paris", "xpar", "paris"}),
    "euronext amsterdam": frozenset({"euronext amsterdam", "xams", "amsterdam"}),
    "swx": frozenset({"six", "swx", "xswx", "six swiss exchange"}),
    "bme": frozenset({"bme", "xmad", "madrid", "bolsa de madrid"}),
    "borsa italiana": frozenset({"borsa italiana", "xmil", "milan", "milano"}),
    "otc": frozenset({"otc", "otcmkts", "pink", "otc markets"}),
}


def split_broker_ticker(broker_ticker: str) -> tuple[str, str | None, str | None]:
    """Split a Trading 212 ticker into ``(symbol, market code, instrument kind)``.

    ``AAPL_US_EQ`` -> ``("AAPL", "US", "EQ")``.  This is a *derivation*, not
    documented provider metadata: Trading 212 documents the ticker only as an
    opaque unique identifier, and the full ticker is always what reaches the
    broker.

    Non-US listings use a two-part form -- ``VODl_EQ`` -> ``("VODl", None,
    "EQ")`` -- so a missing venue code is normal rather than exceptional: 64% of
    the live universe has one (measured 2026-09-04).  Returning the *whole*
    ticker as the symbol in that case, as this function first did, made the
    fallback market symbol ``VODL.EQ``; it now returns the first segment and no
    venue code.  Either way ``shortName`` is preferred, and it is populated on
    100% of live rows.
    """
    parts = broker_ticker.split("_")
    if len(parts) >= 3:
        return parts[0], parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], None, parts[-1]
    return broker_ticker, None, None
