"""Resolving one FX rate, with a policy and no fallbacks.

Modelled on :mod:`stockbrain.proposals.quotes`, deliberately: an FX rate is a
price, and every rule that made quote handling safe applies to it.

* **No cache.**  A rate is fetched when it is needed.  Caching is how a stale
  number gets used after the check that would have rejected it.
* **No fallback provider.**  One configured source.  Silently substituting a
  daily fixing when a live feed is down would change the meaning of a proposal
  without changing anything a reader can see.
* **A refusal is a result.**  :class:`FxResolution` always comes back; it either
  carries a usable conversion or it carries the reasons it does not, and the
  risk engine reads those reasons the same way it reads quote blockers.
* **Missing, stale or untrusted all block.**  There is no path through this
  module that returns a rate of one for two different currencies.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockbrain.config import FxProviderName, Settings
from stockbrain.db.base import utcnow
from stockbrain.errors import ProviderAuthError, ProviderEntitlementError, ProviderError
from stockbrain.fx.base import (
    FxCapability,
    FxRate,
    FxRateGrade,
    FxRateProvider,
    currency_pair,
    fx_blockers,
    normalize_currency,
)
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["FxResolution", "FxService", "build_fx_provider"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FxResolution:
    """The outcome of asking for one rate.

    ``usable`` is defined as "no blockers remain", so this object and the
    reasons it carries cannot disagree.
    """

    from_currency: str
    to_currency: str
    rate: FxRate | None
    blockers: tuple[str, ...]
    same_currency: bool
    provider: str | None
    resolved_at: dt.datetime

    @property
    def usable(self) -> bool:
        return not self.blockers

    @property
    def pair(self) -> str:
        return currency_pair(self.from_currency, self.to_currency)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "pair": self.pair,
            "same_currency": self.same_currency,
            "usable": self.usable,
            "blockers": list(self.blockers),
            "provider": self.provider,
            "resolved_at": self.resolved_at.isoformat(),
            "rate": self.rate.as_dict() if self.rate is not None else None,
        }


def build_fx_provider(settings: Settings) -> FxRateProvider | None:
    """Construct the configured provider, or ``None``.

    ``FX_PROVIDER=none`` is the default and is not a misconfiguration: it means
    cross-currency sizing is blocked, which is exactly the Phase 6 behaviour and
    is safe.  Choosing a source is an operator decision with cost and
    entitlement consequences, so it is never inferred from a credential being
    present.
    """
    if settings.fx_provider is FxProviderName.NONE:
        return None
    if settings.fx_provider is FxProviderName.ALPACA:
        from stockbrain.fx.alpaca import AlpacaFxProvider

        return AlpacaFxProvider(settings)
    if settings.fx_provider is FxProviderName.FRANKFURTER:
        from stockbrain.fx.frankfurter import FrankfurterFxProvider

        return FrankfurterFxProvider(settings)
    raise AssertionError(f"unhandled FX provider {settings.fx_provider}")  # pragma: no cover


class FxService:
    """Fetches and judges one rate.  Decides nothing else."""

    def __init__(self, settings: Settings, *, provider: FxRateProvider | None) -> None:
        self._settings = settings
        self._provider = provider

    @property
    def provider_name(self) -> str | None:
        return self._provider.name if self._provider is not None else None

    @property
    def grade(self) -> FxRateGrade | None:
        return self._provider.grade if self._provider is not None else None

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()

    async def capability(self) -> FxCapability:
        """What FX can currently do.

        With no provider configured this reports unavailable with that reason,
        rather than reporting healthy because nothing failed.
        """
        if self._provider is None:
            return FxCapability(
                provider="none",
                # No grade, rather than a placeholder one: reporting REFERENCE
                # for a provider that does not exist would read as "a fixing is
                # available" in a health panel.
                grade=None,
                blockers=(
                    "FX_PROVIDER is 'none': cross-currency sizing is blocked and no rate "
                    "is ever inferred",
                ),
            )
        return await self._provider.capability()

    async def resolve(
        self, *, from_currency: str, to_currency: str, now: dt.datetime | None = None
    ) -> FxResolution:
        """Fetch and judge the rate needed to convert one currency into another.

        The ordering of the arguments is the conversion's, not the market's:
        callers ask "how do I turn account currency into instrument currency",
        and the provider is free to publish the pair the other way round.
        """
        moment = now or utcnow()
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)

        if not source or not target:
            return FxResolution(
                from_currency=source,
                to_currency=target,
                rate=None,
                blockers=(
                    "the instrument or account currency is unknown, so no conversion is defined",
                ),
                same_currency=False,
                provider=self.provider_name,
                resolved_at=moment,
            )

        if source == target:
            # No rate is fetched and none is needed. Reported as its own state
            # rather than as a rate of one, so a reader can tell "same currency"
            # from "converted at parity".
            return FxResolution(
                from_currency=source,
                to_currency=target,
                rate=None,
                blockers=(),
                same_currency=True,
                provider="identity",
                resolved_at=moment,
            )

        if self._provider is None:
            return FxResolution(
                from_currency=source,
                to_currency=target,
                rate=None,
                blockers=(
                    f"no FX provider is configured, so {source}->{target} cannot be "
                    f"converted; set FX_PROVIDER to enable cross-currency sizing",
                ),
                same_currency=False,
                provider=None,
                resolved_at=moment,
            )

        rate: FxRate | None = None
        missing_reason: str | None = None
        try:
            rate = await self._provider.latest(source, target)
        except (ProviderAuthError, ProviderEntitlementError) as exc:
            missing_reason = (
                f"the {self._provider.name} FX source refused the request "
                f"({type(exc).__name__}); cross-currency sizing is blocked"
            )
            log.warning(
                "fx_rate_unavailable",
                provider=self._provider.name,
                pair=currency_pair(source, target),
                error_type=type(exc).__name__,
            )
        except ProviderError as exc:
            missing_reason = (
                f"the {self._provider.name} FX source is unavailable "
                f"({type(exc).__name__}); cross-currency sizing is blocked"
            )
            log.warning(
                "fx_rate_unavailable",
                provider=self._provider.name,
                pair=currency_pair(source, target),
                error_type=type(exc).__name__,
            )
        except ValueError as exc:
            # A malformed pair or a rate the adapter refused to construct. Not a
            # transport problem, and not something to retry.
            missing_reason = f"the FX source returned an unusable rate: {exc}"
            log.warning(
                "fx_rate_rejected",
                provider=self._provider.name,
                pair=currency_pair(source, target),
                error_type=type(exc).__name__,
            )

        blockers = fx_blockers(
            rate,
            from_currency=source,
            to_currency=target,
            now=moment,
            max_age_seconds=Decimal(str(self._settings.fx_max_age_seconds)),
            reference_max_age_seconds=Decimal(str(self._settings.fx_reference_max_age_seconds)),
            allow_reference_grade=self._settings.fx_allow_reference_grade,
            missing_reason=missing_reason,
        )
        if rate is not None:
            METRICS.set(
                "stockbrain_fx_rate_age_seconds",
                float(rate.age_seconds(moment)),
                labels={"pair": rate.pair, "provider": rate.provider},
            )
        return FxResolution(
            from_currency=source,
            to_currency=target,
            rate=rate,
            blockers=tuple(blockers),
            same_currency=False,
            provider=self.provider_name,
            resolved_at=moment,
        )
