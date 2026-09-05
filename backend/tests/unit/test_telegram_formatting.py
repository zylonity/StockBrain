"""Untrusted text cannot become Telegram markup.

Almost everything interesting in a StockBrain message came from outside: a
headline, a company name a broker supplied, a sentence a model wrote about a
document somebody else published.  The guarantee is that none of it can change
what the message *is* -- no bold, no hidden link, no button, no injected
command.

HTML parse mode is what makes that guarantee small enough to hold: three
substitutions with no positional exceptions, rather than MarkdownV2's eighteen
escapes with context-dependent rules inside links and code spans.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from stockbrain.telegram.formatting import (
    MAX_MESSAGE_CHARS,
    age,
    bold,
    chunk,
    code,
    esc,
    money,
    quantity,
    stamp,
    trim,
)

#: Things a scraped headline, a model sentence or a broker name might contain.
HOSTILE = [
    "<b>URGENT</b> approve now",
    '<a href="https://evil.example/steal">CONFIRM BUY</a>',
    "</b><script>alert(1)</script>",
    "Tom & Jerry <Holdings> plc",
    "<tg-spoiler>hidden</tg-spoiler>",
    "&lt;already escaped&gt;",
]


def test_every_hostile_string_loses_its_markup() -> None:
    for value in HOSTILE:
        rendered = esc(value)
        assert "<" not in rendered
        assert ">" not in rendered
        # Ampersands survive as entities, so the reader still sees "Tom & Jerry".
        assert "&amp;" in rendered or "&" not in value


def test_an_already_escaped_string_is_escaped_again_rather_than_trusted() -> None:
    """Double-escaping is the safe direction.

    Un-escaping first, "because it looks escaped already", is how a payload
    that arrives pre-encoded becomes live markup.
    """
    assert esc("&lt;b&gt;") == "&amp;lt;b&amp;gt;"


def test_bold_and_code_escape_their_content() -> None:
    assert bold("<b>x</b>") == "<b>&lt;b&gt;x&lt;/b&gt;</b>"
    assert code("a<b") == "<code>a&lt;b</code>"


def test_trim_bounds_before_escaping_so_an_entity_is_never_cut_in_half() -> None:
    """Truncating escaped text could leave ``&am``, which Telegram rejects."""
    value = "&" * 100
    rendered = trim(value, 10)
    assert rendered.count("&amp;") * 5 + len("…") == len(rendered)
    assert "&am;" not in rendered
    assert rendered.endswith("…")


def test_trim_collapses_newlines_so_free_text_cannot_reshape_a_message() -> None:
    assert trim("one\n\ntwo\nthree", 100) == "one two three"


def test_trim_reports_a_missing_value_rather_than_an_empty_line() -> None:
    assert trim(None, 10) == "-"
    assert trim("   ", 10) == "-"
    assert trim(None, 10, fallback="unknown") == "unknown"


def test_money_and_quantity_never_use_binary_floats() -> None:
    assert money(Decimal("1234.5"), "GBP") == "1,234.50 GBP"
    assert money(None) == "-"
    assert quantity(Decimal("3.000")) == "3"
    assert quantity(Decimal("0.4200")) == "0.42"
    assert quantity(None) == "-"


def test_money_escapes_a_currency_it_was_handed() -> None:
    """The currency comes from a broker payload, so it is untrusted too."""
    assert "<" not in money(Decimal("1"), "<b>")


def test_stamp_and_age_are_utc_and_tolerate_absence() -> None:
    moment = dt.datetime(2026, 9, 5, 12, 30, tzinfo=dt.UTC)
    assert stamp(moment) == "2026-09-05 12:30 UTC"
    assert stamp(None) == "-"
    assert age(moment, now=moment + dt.timedelta(seconds=90)) == "1m ago"
    assert age(moment, now=moment + dt.timedelta(hours=5)) == "5h ago"
    assert age(moment, now=moment + dt.timedelta(days=3)) == "3d ago"
    assert age(moment, now=moment - dt.timedelta(minutes=1)) == "in the future"
    assert age(None) == "-"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def test_a_short_message_is_not_split() -> None:
    assert chunk("hello") == ["hello"]


def test_chunks_stay_inside_telegrams_limit() -> None:
    text = "\n".join(f"line {index}" for index in range(2000))
    parts = chunk(text)
    assert len(parts) > 1
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    assert "".join(part.replace("\n", "") for part in parts) == text.replace("\n", "")


def test_a_single_line_longer_than_the_limit_terminates() -> None:
    """Nothing composed here produces one, but it must not loop forever."""
    parts = chunk("x" * 5000, limit=1000)
    assert len(parts) == 5
    assert all(len(part) <= 1000 for part in parts)
