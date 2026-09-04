"""URL canonicalisation, HTML extraction and content hashing.

These are the deterministic foundations of deduplication: if canonicalisation is
wrong, the same story is ingested repeatedly, and if it is too aggressive, two
different stories are merged. Both failure modes are tested.
"""

from __future__ import annotations

import pytest

from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.normalizer import (
    canonicalize_url,
    classify_source_category,
    content_hash,
    html_to_text,
    normalize_document,
    normalize_whitespace,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("HTTPS://EXAMPLE.COM/a", "https://example.com/a"),
        ("https://www.example.com/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_canonicalization_collapses_equivalent_forms(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_tracking_parameters_are_removed_but_real_ones_survive() -> None:
    url = "https://example.com/a?utm_source=x&utm_medium=y&fbclid=z&id=42&page=2"
    assert canonicalize_url(url) == "https://example.com/a?id=42&page=2"


def test_query_parameters_are_sorted_so_order_does_not_matter() -> None:
    assert canonicalize_url("https://example.com/a?b=2&a=1") == canonicalize_url(
        "https://example.com/a?a=1&b=2"
    )


def test_path_case_is_preserved() -> None:
    """Hosts are case-insensitive; paths are not. Lowercasing a path breaks URLs."""
    assert canonicalize_url("https://EXAMPLE.com/Article/Id") == "https://example.com/Article/Id"


@pytest.mark.parametrize(
    "raw",
    ["", None, "not a url", "/relative", "mailto:a@b.c", "javascript:alert(1)", "ftp://x/y"],
)
def test_non_http_urls_have_no_canonical_identity(raw: str | None) -> None:
    assert canonicalize_url(raw) is None


def test_different_documents_stay_different() -> None:
    assert canonicalize_url("https://example.com/a") != canonicalize_url("https://example.com/b")


def test_html_to_text_drops_scripts_and_styles() -> None:
    html = "<p>Real text</p><script>steal()</script><style>.x{}</style><p>More</p>"
    text = html_to_text(html)
    assert "Real text" in text
    assert "More" in text
    assert "steal" not in text
    assert ".x{}" not in text


def test_html_to_text_unescapes_entities_and_breaks_blocks() -> None:
    assert html_to_text("<p>A &amp; B</p><p>C</p>") == "A & B\nC"
    assert html_to_text("one<br>two") == "one\ntwo"


def test_content_hash_ignores_case_and_whitespace() -> None:
    assert content_hash("Acme  Wins", "Body  text") == content_hash("acme wins", "body text")


def test_content_hash_distinguishes_real_differences() -> None:
    assert content_hash("Acme Wins", "a") != content_hash("Acme Loses", "a")
    assert content_hash("Acme Wins", "a") != content_hash("Acme Wins", "b")


def test_normalize_whitespace_handles_unicode_spaces() -> None:
    # U+00A0 NO-BREAK SPACE and U+200B ZERO WIDTH SPACE, written as escapes:
    # scraped articles are full of both and they must not defeat hashing.
    assert normalize_whitespace("a\u00a0b\u200bc") == "a b c"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.sec.gov/Archives/x", SourceCategory.REGULATOR),
        ("https://data.sec.gov/submissions/x", SourceCategory.REGULATOR),
        ("https://www.benzinga.com/news/x", SourceCategory.NEWSWIRE),
        ("https://reuters.com/business/x", SourceCategory.NEWSWIRE),
        ("https://www.cnbc.com/x", SourceCategory.PRESS),
        ("https://someone.substack.com/p/x", SourceCategory.UNKNOWN),
        (None, SourceCategory.UNKNOWN),
    ],
)
def test_source_categories_are_a_transparent_lookup(
    url: str | None, expected: SourceCategory
) -> None:
    assert classify_source_category(url) is expected


def test_normalize_document_produces_every_dedupe_field() -> None:
    document = RawSourceDocument(
        provider=SourceProvider.ALPACA,
        url="https://www.benzinga.com/story?utm_source=feed",
        headline="Acme  Wins Contract",
        body="<p>Details <b>here</b></p>",
    )
    normalized = normalize_document(document)

    assert normalized.canonical_url == "https://benzinga.com/story"
    assert normalized.normalized_text == "Details here"
    assert normalized.normalized_headline == "acme wins contract"
    assert len(normalized.content_hash) == 64
    assert normalized.source_category is SourceCategory.NEWSWIRE


def test_explicit_source_category_is_not_overridden() -> None:
    """A provider that knows its own category (SEC) must keep it."""
    document = RawSourceDocument(
        provider=SourceProvider.SEC,
        url="https://example.com/x",
        source_category=SourceCategory.REGULATOR,
        headline="Filing",
    )
    assert normalize_document(document).source_category is SourceCategory.REGULATOR
