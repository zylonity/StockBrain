"""Bid/ask spread assessment.

This module exists because of one live observation.  At 03:40 UTC Alpaca's
"latest" IEX quote for AAPL was the 16:00 ET closing print, with a **$33 spread
on a $321 mid** -- a 10% round trip.  The quote-age check happened to catch that
particular quote, but a book that wide *during* regular hours would pass an age
check and still be ruinous to trade against.  Age and width are independent
facts and both must be checked.

Two implementation rules:

* **Every value is a :class:`~decimal.Decimal`.**  ``0.1 + 0.2`` is not ``0.3``,
  and a threshold comparison is exactly where that stops being an amusing fact.
* **The threshold comparison is exact.**  ``spread / mid`` is frequently a
  non-terminating decimal, so comparing a rounded ratio against a limit would
  make the boundary depend on the rounding.  The test is done by
  cross-multiplication -- ``spread * 10000 <= ceiling_bps * mid`` -- which is
  pure multiplication and therefore exact.  The rounded percentage and basis
  points are computed afterwards, for display and persistence only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockbrain.enums import SpreadStatus

__all__ = ["SpreadAssessment", "assess_spread"]

_BPS = Decimal(10000)
_PCT_EXPONENT = Decimal("0.00000001")
_BPS_EXPONENT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class SpreadAssessment:
    """The width of a book, and whether it may be traded against."""

    status: SpreadStatus
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    mid: Decimal | None = None
    spread_pct: Decimal | None = None
    spread_bps: Decimal | None = None
    ceiling_bps: Decimal | None = None
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status is SpreadStatus.OK

    @property
    def is_merely_wide(self) -> bool:
        """A well-formed book that is only too wide.

        The one status a ``REDUCE`` spread policy may soften.  Everything else
        describes a book with no usable mid at all, which no policy may trade.
        """
        return self.status is SpreadStatus.EXCESSIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "mid": str(self.mid) if self.mid is not None else None,
            "spread_pct": str(self.spread_pct) if self.spread_pct is not None else None,
            "spread_bps": str(self.spread_bps) if self.spread_bps is not None else None,
            "ceiling_bps": str(self.ceiling_bps) if self.ceiling_bps is not None else None,
            "detail": self.detail,
        }


def assess_spread(
    bid: Decimal | None,
    ask: Decimal | None,
    *,
    max_spread_bps: Decimal,
) -> SpreadAssessment:
    """Classify a bid/ask pair against a relative-width ceiling.

    ``max_spread_bps`` is basis points of the mid.  The boundary is
    **inclusive**: a spread exactly at the ceiling passes, so a configured
    "50 bps" means "no wider than 50 bps" rather than "strictly narrower".

    Abnormal shapes are named rather than merged, because their causes differ:

    ==================  =================================================
    ``MISSING``         a side is absent from the payload
    ``NON_POSITIVE``    both sides present, both zero or negative
    ``ONE_SIDED``       exactly one side is a positive price
    ``CROSSED``         ``ask < bid``
    ``LOCKED``          ``ask == bid``
    ``EXCESSIVE``       well-formed, but wider than the ceiling
    ==================  =================================================

    A crossed or locked book is refused rather than treated as a zero-cost
    round trip: a two-sided equity quote with no width is an artefact of a
    halt, a feed update or a venue's own bookkeeping, not free liquidity.
    """
    ceiling = max_spread_bps

    if bid is None or ask is None:
        return SpreadAssessment(
            status=SpreadStatus.MISSING,
            bid=bid,
            ask=ask,
            ceiling_bps=ceiling,
            detail="quote is missing a bid or an ask",
        )

    bid_live = bid > 0
    ask_live = ask > 0
    if not bid_live and not ask_live:
        return SpreadAssessment(
            status=SpreadStatus.NON_POSITIVE,
            bid=bid,
            ask=ask,
            ceiling_bps=ceiling,
            detail="neither side of the quote carries a positive price",
        )
    if not (bid_live and ask_live):
        return SpreadAssessment(
            status=SpreadStatus.ONE_SIDED,
            bid=bid,
            ask=ask,
            ceiling_bps=ceiling,
            detail="only one side of the quote carries a positive price",
        )

    spread = ask - bid
    mid = (bid + ask) / Decimal(2)

    if spread < 0:
        return SpreadAssessment(
            status=SpreadStatus.CROSSED,
            bid=bid,
            ask=ask,
            spread=spread,
            mid=mid,
            ceiling_bps=ceiling,
            detail=f"crossed market: ask {ask} is below bid {bid}",
        )
    if spread == 0:
        return SpreadAssessment(
            status=SpreadStatus.LOCKED,
            bid=bid,
            ask=ask,
            spread=spread,
            mid=mid,
            spread_pct=Decimal(0),
            spread_bps=Decimal(0),
            ceiling_bps=ceiling,
            detail=f"locked market: bid and ask are both {bid}",
        )

    # mid > 0 is guaranteed here: both sides are positive.
    spread_pct = (spread / mid).quantize(_PCT_EXPONENT)
    spread_bps = (spread / mid * _BPS).quantize(_BPS_EXPONENT)

    # Exact comparison by cross-multiplication; see the module docstring.
    if spread * _BPS > ceiling * mid:
        return SpreadAssessment(
            status=SpreadStatus.EXCESSIVE,
            bid=bid,
            ask=ask,
            spread=spread,
            mid=mid,
            spread_pct=spread_pct,
            spread_bps=spread_bps,
            ceiling_bps=ceiling,
            detail=(
                f"spread {spread_bps} bps exceeds the {ceiling} bps ceiling (bid {bid} / ask {ask})"
            ),
        )

    return SpreadAssessment(
        status=SpreadStatus.OK,
        bid=bid,
        ask=ask,
        spread=spread,
        mid=mid,
        spread_pct=spread_pct,
        spread_bps=spread_bps,
        ceiling_bps=ceiling,
        detail=f"spread {spread_bps} bps within the {ceiling} bps ceiling",
    )
