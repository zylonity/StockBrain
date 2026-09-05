"""Frankfurter: central-bank reference rates, no key, explicitly not a dealing rate.

Verified live 2026-09-05 against <https://frankfurter.dev/>:

* ``GET https://api.frankfurter.dev/v2/rates?base=GBP&quotes=USD``
* no API key, no quota; "requests are rate-limited to prevent abuse, but there
  are no monthly or daily caps"
* response is a JSON **array**: ``[{"date":"2026-09-05","base":"GBP",
  "quote":"USD","rate":1.3521}]``
* the service aggregates "daily exchange rates from 84 central banks", ECB
  included, and its own documentation states it "is not for live trading"

That last sentence is why this provider is
:attr:`~stockbrain.fx.base.FxRateGrade.REFERENCE` and why a reference-grade rate
cannot size a trade unless ``FX_ALLOW_REFERENCE_GRADE`` is explicitly true.  It
is a published fixing, not a price anyone will deal on.  Sizing a small equity
position against a fixing that is hours old is defensible; pretending it is a
dealable quote is not, and the difference has to be visible on the proposal.

**Two conservative decisions about the timestamp.**  The response carries a
*date* and no time.  A fixing is published during the day it is dated, so the
honest bound on its age is the widest one: the provider timestamp is taken as
**00:00 UTC on the returned date**, which over-states the age and can only ever
make a freshness check stricter.  And because central banks do not publish at
weekends or on holidays, a Monday-morning rate is dated the previous Friday --
so cross-currency sizing blocks over a weekend unless the reference age limit is
raised deliberately.  Equity markets are closed then anyway.

The pair is requested directly (``base`` and ``quotes``) rather than assembled
from two EUR crosses.  StockBrain never chains rates; if the value returned is
itself a provider-computed cross, that is the provider's published number, and
the recorded ``rate_type`` says which service published it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.fx.base import (
    FxCapability,
    FxRate,
    FxRateGrade,
    currency_pair,
    normalize_currency,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.logging import get_logger

__all__ = ["FRANKFURTER_RATES_PATH", "FrankfurterFxProvider"]

log = get_logger(__name__)

FRANKFURTER_RATES_PATH = "/v2/rates"


class FrankfurterFxProvider:
    """Daily central-bank reference rates."""

    name = "frankfurter"
    grade = FxRateGrade.REFERENCE

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="frankfurter",
            base_url=settings.fx_frankfurter_base_url,
            headers={"Accept": "application/json"},
            timeout_seconds=15.0,
            # No documented quota, but a rate this system consults a handful of
            # times an hour has no reason to arrive in a burst.
            rate_limiter=TokenBucket(rate_per_second=1.0, burst=3),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def latest(self, base: str, quote: str) -> FxRate:
        source = normalize_currency(base)
        target = normalize_currency(quote)
        if not source or not target:
            raise ValueError("both currencies are required")
        if source == target:
            raise ValueError("a same-currency pair is not a rate; use identity_rate()")

        payload = await self._http.get_json(
            FRANKFURTER_RATES_PATH, params={"base": source, "quotes": target}
        )
        received_at = utcnow()

        # A JSON array, not an object. Worth stating: the v1 endpoint returns
        # `{"base": ..., "rates": {...}}` and v2 returns a list of pair rows, so
        # a client written against the older shape finds no "rates" key at all.
        if not isinstance(payload, list) or not payload:
            raise ProviderResponseError("frankfurter: /v2/rates did not return a non-empty array")
        entry = payload[0]
        if not isinstance(entry, dict):
            raise ProviderResponseError("frankfurter: rate entry was not an object")

        entry_base = normalize_currency(str(entry.get("base", "")))
        entry_quote = normalize_currency(str(entry.get("quote", "")))
        if {entry_base, entry_quote} != {source, target}:
            # Refusing rather than reinterpreting: a response about a pair
            # nobody asked for is a response that must not be used to size
            # anything.
            raise ProviderResponseError(
                f"frankfurter: asked for {currency_pair(source, target)} and received "
                f"{currency_pair(entry_base, entry_quote)}"
            )

        rate = _decimal(entry.get("rate"))
        if rate is None:
            raise ProviderResponseError(
                f"frankfurter: {currency_pair(entry_base, entry_quote)} carried no positive rate"
            )
        published = _parse_date(entry.get("date"))
        if published is None:
            raise ProviderResponseError("frankfurter: rate entry carried no usable date")

        return FxRate(
            base_currency=entry_base,
            quote_currency=entry_quote,
            rate=rate,
            provider=self.name,
            grade=self.grade,
            # Floored to the start of the published day: see the module
            # docstring. Over-stating the age is the safe direction.
            provider_timestamp=published,
            received_at=received_at,
            rate_type="central_bank_reference",
            provider_timestamp_precision="day",
        )

    async def capability(self) -> FxCapability:
        blockers: list[str] = []
        detail: str | None = None
        probe_base = normalize_currency(self._settings.fx_probe_base_currency)
        probe_quote = normalize_currency(self._settings.fx_probe_quote_currency)
        try:
            rate = await self.latest(probe_base, probe_quote)
        except ProviderError as exc:
            blockers.append(f"the Frankfurter API is unavailable ({type(exc).__name__})")
            detail = str(exc)[:200]
        else:
            detail = (
                f"{rate.pair} available as a {rate.rate_type} fixing dated "
                f"{rate.provider_timestamp.date().isoformat()}"
            )
            if not self._settings.fx_allow_reference_grade:
                # Not an outage. The source works and is deliberately not
                # trusted to size a trade, which a health panel should say in
                # those words rather than showing a green light.
                blockers.append(
                    "FX_ALLOW_REFERENCE_GRADE is false, so this reference-grade "
                    "fixing may not size a trade"
                )
        return FxCapability(
            provider=self.name, grade=self.grade, blockers=tuple(blockers), detail=detail
        )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_date(value: Any) -> dt.datetime | None:
    """Parse ``YYYY-MM-DD`` into midnight UTC on that date."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.date.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.datetime.combine(parsed, dt.time.min, tzinfo=dt.UTC)
