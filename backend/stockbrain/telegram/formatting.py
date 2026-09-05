"""Rendering StockBrain state into Telegram messages, safely.

**Parse mode is HTML, not MarkdownV2.**  Telegram's MarkdownV2 requires
escaping eighteen characters (``_ * [ ] ( ) ~ ` > # + - = | { } . !``) with
context-dependent extra rules inside links, code spans and custom emoji, and a
single missed one is a ``400 Bad Request`` on the message that mattered.  HTML
mode needs exactly three substitutions -- ``&`` ``<`` ``>`` -- with no
positional exceptions, so the escape is total and reviewable in one function.
That matters here because almost everything interesting in a message is
untrusted: a company name, a headline, a thesis sentence, a broker-supplied
listing name.

Consequences, all deliberate:

* every untrusted value goes through :func:`esc`; the module exposes no way to
  emit an unescaped one;
* untrusted text never becomes a link.  ``<a href=...>`` is built only from
  literals in this module, so a headline containing ``http://...`` renders as
  the characters a human typed and cannot become a clickable destination that
  Telegram will preview;
* untrusted values are length-bounded *before* escaping, then the finished
  message is split on line boundaries against Telegram's 4096-character limit,
  so an entity can never be cut in half.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import Decimal

__all__ = [
    "MAX_MESSAGE_CHARS",
    "age",
    "bold",
    "chunk",
    "code",
    "esc",
    "money",
    "quantity",
    "stamp",
    "trim",
]

#: Telegram documents ``sendMessage.text`` as 1-4096 characters after entity
#: parsing.  Chunking happens against the *rendered* string for that reason.
MAX_MESSAGE_CHARS = 4096

#: Leaves room for the closing tags a chunk boundary might otherwise split.
_CHUNK_TARGET = 3900


def esc(value: object) -> str:
    """Escape a value for Telegram HTML parse mode.

    ``&``, ``<`` and ``>`` are the only characters Telegram requires escaping
    outside a tag, and ``html.escape(quote=False)`` is exactly those three.
    Everything that reaches a message body goes through here, including values
    StockBrain itself produced: a rule that has an exception is a rule somebody
    will forget.
    """
    return html.escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def trim(value: object | None, limit: int, *, fallback: str = "-") -> str:
    """Bound an untrusted string, then escape it.

    Bounding first is what keeps the escape total: truncating the *escaped*
    text could cut ``&amp;`` into ``&am``, which Telegram rejects.
    """
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    # Collapse newlines: a thesis paragraph inside a summary line would break
    # the layout of every message that embeds one.
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return esc(text)


def money(value: Decimal | float | None, currency: str | None = None, *, places: int = 2) -> str:
    if value is None:
        return "-"
    amount = f"{Decimal(str(value)):,.{places}f}"
    return f"{amount} {esc(currency)}" if currency else amount


def quantity(value: Decimal | float | None) -> str:
    """Render a share quantity without inventing precision it does not have."""
    if value is None:
        return "-"
    dec = Decimal(str(value)).normalize()
    if dec == dec.to_integral_value():
        dec = dec.to_integral_value()
    return f"{dec:f}"


def stamp(value: dt.datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def age(value: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    if value is None:
        return "-"
    delta = (now or dt.datetime.now(dt.UTC)) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def chunk(text: str, limit: int = _CHUNK_TARGET) -> list[str]:
    """Split a rendered message on line boundaries to fit Telegram's limit.

    Chunking is a fallback, not a strategy: every message this bot composes is
    bounded by construction (fixed rows, trimmed free text, capped list
    lengths), because a research report dumped across nine messages is not a
    notification anybody can act on.  This exists so that an unusually long
    value degrades into two readable messages rather than a 400 from Telegram.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        # A single line longer than the limit is hard-split; nothing composed
        # here produces one, but a pathological value must not loop forever.
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
