"""Headline checks for whether a story is a fresh catalyst or a report on a move.

A BUY placed because of an article *about* a price move ("Why is X stock
rising?") enters after the move it describes; recaps, call transcripts and
opinion pieces carry no new information. Offline tests over 2019-2026 and the
live record both show buying after the move loses money, so these headlines
can be refused as BUY triggers. Exits are never affected.
"""

from __future__ import annotations

import re

__all__ = ["headline_entry_problem"]

_REACTION = re.compile(
    r"what'?s going on|why is .{0,60}\b(stock|shares)\b|"
    r"\b(stock|shares)\b.{0,20}\b(is |are )?(rising|falling|gaining|soaring|jumping|"
    r"surging|sliding|sinking|trading (higher|lower)|moving)\b|"
    r"\bhits? (a )?(fresh |new )?(record|all-time) high|stock market today|"
    r"\b(top|biggest) (gainers|losers|movers)\b|\bmid-day movers\b|"
    r"\b(up|down|gains?|jumps?|soars?|surges?|rises?|falls?|drops?) "
    r"(over |nearly |about )?\d+(\.\d+)?%",
    re.IGNORECASE,
)
_STALE = re.compile(
    r"\btranscript\b|\brecap\b|"
    r"\b\d+ stocks? (to|analysts|that|you)\b|\btop \d+\b|\bvs\.? ",
    re.IGNORECASE,
)


def headline_entry_problem(headline: str | None) -> str | None:
    """Why this headline should not trigger a BUY, or ``None`` if it may."""
    if not headline:
        return None
    if _STALE.search(headline):
        return "recap, transcript or comparison piece, not a new catalyst"
    if _REACTION.search(headline):
        return "reports a price move that has already happened"
    return None
