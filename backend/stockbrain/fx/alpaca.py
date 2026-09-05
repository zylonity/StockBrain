"""Alpaca forex rates: the execution-grade FX source.

Verified against Alpaca's current API reference (2026-09-05,
<https://docs.alpaca.markets/reference/latestrates-1>):

* ``GET https://data.alpaca.markets/v1beta1/forex/latest/rates``
* required query parameter ``currency_pairs``, comma-separated concatenated
  pairs in market convention (``GBPUSD``, ``USDJPY``, ``USDMXN``)
* the same ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers as the equity
  endpoints
* response ``{"rates": {"GBPUSD": {"bp": ..., "mp": ..., "ap": ...,
  "t": "2022-05-20T05:38:41.311530885Z"}}}`` -- bid, mid, ask and an ISO 8601
  instant with nanosecond precision

**Measured entitlement, 2026-09-05:** this account answers
``HTTP 403 {"message":"forbidden: insufficient grants"}``.  Forex is not
included in the plan that covers IEX equity data, so on the current
subscription this provider is *configured and unavailable* -- which is why
:meth:`AlpacaFxProvider.capability` exists and why the alternative provider
does too.  The finding is recorded in ``docs/sources.md``; nothing here assumes
the entitlement, and the 403 is refined the same way the equity client refines
it so a missing plan degrades rather than looking like a bad credential.

Both orderings of the pair are requested in one call.  Market convention
decides which one Alpaca publishes -- GBPUSD exists, USDGBP conventionally does
not -- and asking for both in a single request costs one call instead of two
guesses.  Whichever comes back is used, and
:meth:`~stockbrain.fx.base.FxRate.convert` inverts it exactly if the caller
needed the other direction.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderResponseError,
)
from stockbrain.fx.base import (
    FxCapability,
    FxRate,
    FxRateGrade,
    currency_pair,
    normalize_currency,
)
from stockbrain.httpclient import ProviderHttpClient, TokenBucket
from stockbrain.logging import get_logger
from stockbrain.market_data.alpaca import refine_alpaca_error

__all__ = ["ALPACA_FX_LATEST_PATH", "AlpacaFxProvider"]

log = get_logger(__name__)

ALPACA_FX_LATEST_PATH = "/v1beta1/forex/latest/rates"


class AlpacaFxProvider:
    """Live forex mid/bid/ask from Alpaca's market-data API."""

    name = "alpaca_fx"
    grade = FxRateGrade.EXECUTION

    def __init__(self, settings: Settings, *, http: ProviderHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or ProviderHttpClient(
            provider="alpaca_fx",
            base_url=settings.alpaca_data_base_url,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
                "Accept": "application/json",
            },
            timeout_seconds=15.0,
            # Deliberately a tenth of the equity client's rate. Alpaca's
            # 200 requests/minute is per *account*, and this client holds its own
            # bucket -- two independent limiters against one shared budget could
            # together exceed it. FX makes a handful of calls an hour (a
            # proposal, an authorization, a send, a sweep), so it costs nothing
            # to leave the headroom to the path that actually needs throughput.
            rate_limiter=TokenBucket(rate_per_second=0.2, burst=2),
            # Reused deliberately: the equity client and this one hit the same
            # host with the same credential and get the same ambiguous 403 for a
            # bad key and a missing plan.
            refine_error=refine_alpaca_error,
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

        direct = currency_pair(source, target)
        inverse = currency_pair(target, source)
        payload = await self._http.get_json(
            ALPACA_FX_LATEST_PATH,
            params={"currency_pairs": f"{direct},{inverse}"},
        )
        received_at = utcnow()

        if not isinstance(payload, dict):
            raise ProviderResponseError("alpaca_fx: response was not a JSON object")
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            raise ProviderResponseError("alpaca_fx: response had no 'rates' object")

        for pair, (pair_base, pair_quote) in (
            (direct, (source, target)),
            (inverse, (target, source)),
        ):
            entry = rates.get(pair)
            if isinstance(entry, dict):
                return self._to_rate(entry, pair_base, pair_quote, received_at)

        raise ProviderResponseError(
            f"alpaca_fx: neither {direct} nor {inverse} was returned; "
            f"the pair is not published by this feed"
        )

    def _to_rate(
        self,
        entry: dict[str, Any],
        base: str,
        quote: str,
        received_at: dt.datetime,
    ) -> FxRate:
        mid = _decimal(entry.get("mp"))
        bid = _decimal(entry.get("bp"))
        ask = _decimal(entry.get("ap"))
        if mid is None:
            # A mid is what sizing uses. Deriving one from bid and ask is
            # arithmetic on published numbers, not an assumption, so it is
            # allowed -- but only when both sides are present.
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                mid = (bid + ask) / Decimal(2)
            else:
                raise ProviderResponseError(
                    f"alpaca_fx: {currency_pair(base, quote)} carried no usable mid price"
                )
        timestamp = _parse_instant(entry.get("t"))
        if timestamp is None:
            # A rate with no provider timestamp cannot be checked for
            # freshness, and an unfreshenable rate may not size anything.
            raise ProviderResponseError(
                f"alpaca_fx: {currency_pair(base, quote)} carried no timestamp"
            )
        return FxRate(
            base_currency=base,
            quote_currency=quote,
            rate=mid,
            provider=self.name,
            grade=self.grade,
            provider_timestamp=timestamp,
            received_at=received_at,
            rate_type="mid",
            bid=bid,
            ask=ask,
            provider_timestamp_precision="instant",
        )

    async def capability(self) -> FxCapability:
        """One request against a major pair, to measure the entitlement.

        Measured rather than assumed, because "the credential works for equities"
        does not imply "the plan includes forex" -- and on this account it
        demonstrably does not.
        """
        blockers: list[str] = []
        detail: str | None = None
        if not (
            self._settings.alpaca_api_key.get_secret_value()
            and self._settings.alpaca_api_secret.get_secret_value()
        ):
            return FxCapability(
                provider=self.name,
                grade=self.grade,
                blockers=("Alpaca API credentials are not configured",),
            )
        probe_base = normalize_currency(self._settings.fx_probe_base_currency)
        probe_quote = normalize_currency(self._settings.fx_probe_quote_currency)
        try:
            rate = await self.latest(probe_base, probe_quote)
        except ProviderEntitlementError as exc:
            blockers.append(
                "the Alpaca subscription does not include forex data "
                "(HTTP 403 'insufficient grants')"
            )
            detail = str(exc)[:200]
        except ProviderAuthError as exc:
            blockers.append("Alpaca rejected the credential for the forex endpoint")
            detail = str(exc)[:200]
        except ProviderError as exc:
            blockers.append(f"the Alpaca forex endpoint is unavailable ({type(exc).__name__})")
            detail = str(exc)[:200]
        else:
            detail = f"{rate.pair} available, {rate.rate_type} price"
        return FxCapability(
            provider=self.name, grade=self.grade, blockers=tuple(blockers), detail=detail
        )


def _decimal(value: Any) -> Decimal | None:
    """Parse a provider number as ``Decimal`` via ``str``.

    Never ``Decimal(float)``: the float has already lost the digits by then, and
    a rate is a financial value.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_instant(value: Any) -> dt.datetime | None:
    """Parse Alpaca's RFC 3339 timestamp, which carries nanoseconds.

    ``datetime.fromisoformat`` accepts at most microseconds, so the fractional
    part is truncated to six digits rather than rejected.  Truncating loses
    nanoseconds from a freshness check measured in seconds; rejecting would lose
    the rate.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                rest = tail[index:]
                break
        text = f"{head}.{digits[:6]}{rest}"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
