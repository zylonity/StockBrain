"""Normalisation helpers shared by the intelligence layer."""

from __future__ import annotations

import re
import unicodedata

__all__ = ["company_key"]

_SUFFIXES = (
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
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def company_key(name: str | None) -> str:
    """Stable key for a company name, used to deduplicate impact rows.

    Case, punctuation, accents and common legal suffixes are removed, so
    "Vertiv Holdings Co.", "vertiv holdings" and "VERTIV HOLDINGS CO" collapse to
    one key. This is a *deduplication* key for one event's impact rows -- it is
    not instrument resolution, which happens later against verified broker
    metadata.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _NON_ALNUM.sub(" ", text.lower()).strip()
    if not text:
        return ""

    words = text.split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)
