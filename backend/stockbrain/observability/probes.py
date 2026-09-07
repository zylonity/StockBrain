"""Startup self-tests: one cheap probe per enabled provider.

Why this exists.  Every provider except PostgreSQL used to reach ``HEALTHY``
only as a *side effect of doing real work* -- Trading 212 on its instrument
sync, Alpaca on its capability probe, Telegram on ``getMe``, Brave and Exa on
an actual discovery sweep.  A freshly restarted container therefore reported
``UNKNOWN`` for most of the board, and :meth:`ProviderHealthRegistry.
overall_status` downgrades to ``DEGRADED`` on *any* ``UNKNOWN`` -- so a
perfectly healthy deployment read as degraded until its first scheduled sweep,
which for the semantic queries is a day away.  An operator cannot tell that
apart from a real fault, which makes the board worth less than no board.

The rule every probe here obeys: **a probe must never cost a billable search.**

That is not frugality.  Exa's default allowance is three searches a day, so
probing it by searching would let three container restarts spend the entire
daily semantic budget on health checks and leave the real queries deferring --
a health check that breaks the feature it reports on.  ``provider_budget``
exists because that class of accident already happened once here, with
Firecrawl, and it is not being reintroduced in a new place.

So the metered providers are probed for **credential validity** rather than by
doing their work.  Each adapter owns the knowledge of its own auth contract
(``verify_credentials``); this module only decides who to ask and how to
report it:

* Brave answers 422 both for a missing ``q`` and for a rejected token, naming
  the difference in the body (measured 2026-09-06).
* Exa answers 400 for an unacceptable body and 401 for a rejected key.
* DeepSeek answers ``GET /models`` for free.

What that buys is the failure that actually happens -- a key that is missing,
mistyped, revoked, or pointed at the wrong project.  What it deliberately does
*not* re-prove on every restart is response parsing, which the
recorded-payload unit tests cover permanently and
``tests/integration/test_web_discovery_live.py`` covers live on demand -- which
is also the answer for an operator who wants end-to-end proof rather than a
credential check.  ``pytest -m live -s tests/integration/test_web_discovery_live.py``
buys one real search per provider, deliberately, on a human's decision rather
than on every container restart; there is no configuration switch for it here
because a switch would be a second, worse spelling of that test.  A
probe reports what it measured and no more, which is why a credential-only
result says so in its detail rather than claiming a search succeeded.

Two providers are deliberately not probed:

* **Firecrawl**, and anything else ``register_static_provider_states`` already
  marked ``DISABLED``.  "Every enabled component" is the rule; probing a
  switched-off provider would be spending to learn nothing.
* **Alpaca news**, which is a long-lived websocket rather than a
  request/response call.  It reports ``UNKNOWN "connecting"`` and flips itself
  to ``HEALTHY`` within seconds of the stream landing, so blocking startup on
  it would trade a real signal for a slower boot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stockbrain.db.base import utcnow
from stockbrain.enums import CapabilityState, ProviderStatus
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
)
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderName

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from stockbrain.services import ServiceContainer

__all__ = [
    "CREDENTIAL_ONLY_DETAIL",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeOutcome",
    "build_probe_plan",
    "run_probe",
]

log = get_logger(__name__)

#: Per-probe ceiling.  Probes run concurrently, so this is also very nearly the
#: worst case the whole sweep adds to startup.  Generous enough for a cold TLS
#: handshake to a slow provider, short enough that a hanging provider cannot
#: hold the container in "starting" past its healthcheck grace period.
PROBE_TIMEOUT_SECONDS = 10.0

#: A credential probe proves reachability and the key, and says so.  The wording
#: matters: an operator reading ``HEALTHY`` needs to know whether the provider
#: was asked to do its actual job or merely to confirm it would accept the
#: request.
CREDENTIAL_ONLY_DETAIL = "credentials accepted; no billable request made"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What one probe measured.  Never an exception -- see :func:`run_probe`."""

    provider: ProviderName
    status: ProviderStatus
    detail: str | None = None
    metrics: dict[str, Any] | None = None


#: One provider's probe: a coroutine function taking nothing and returning the
#: detail line to record on success.  Failure is expressed by raising, which
#: :func:`run_probe` classifies -- so a probe body stays as short as the call it
#: is wrapping.
Probe = Callable[[], Awaitable[str | None]]


async def run_probe(
    provider: ProviderName,
    probe: Probe,
    *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeOutcome:
    """Run one probe and turn whatever happens into an outcome.

    This never raises and never lets a probe run unbounded.  Both properties
    are load-bearing: these run during ``lifespan``, and an exception or a hang
    here would take down or stall a container over a third party being briefly
    unavailable -- exactly the coupling the subsystem health model exists to
    avoid.

    The classification distinguishes the three things an operator does
    different things about:

    * ``DOWN`` -- a credential or entitlement problem.  Nothing will work until
      a human changes configuration.
    * ``DEGRADED`` -- reachable but unhappy right now (rate limited, timed out,
      transport failure).  Likely to fix itself; the feature is impaired.
    * ``HEALTHY`` -- the probe got the answer it asked for.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            detail = await probe()
    except TimeoutError:
        log.warning("provider_probe_timeout", provider=provider.value, timeout=timeout_seconds)
        return ProbeOutcome(
            provider,
            ProviderStatus.DEGRADED,
            detail=f"probe did not answer within {timeout_seconds:g}s",
        )
    except (ProviderAuthError, ProviderEntitlementError) as exc:
        # A configuration fault, not an outage. Reported as DOWN so it is
        # visibly an operator's problem rather than something to wait out.
        log.error("provider_probe_rejected", provider=provider.value, error=str(exc))
        return ProbeOutcome(provider, ProviderStatus.DOWN, detail=str(exc)[:300])
    except ProviderRateLimited as exc:
        return ProbeOutcome(provider, ProviderStatus.DEGRADED, detail=str(exc)[:300])
    except Exception as exc:
        log.warning(
            "provider_probe_failed",
            provider=provider.value,
            error=f"{type(exc).__name__}: {exc}",
        )
        return ProbeOutcome(
            provider, ProviderStatus.DEGRADED, detail=f"{type(exc).__name__}: {exc}"[:300]
        )
    return ProbeOutcome(provider, ProviderStatus.HEALTHY, detail=detail)


def build_probe_plan(services: ServiceContainer) -> dict[ProviderName, Probe]:
    """Which providers this deployment can probe, and how.

    Only clients that were actually constructed appear: ``ServiceContainer``
    builds each one behind its own configuration gate, so a ``None`` attribute
    already means "this deployment cannot make that call".  Reading it that way
    keeps the enabled/disabled decision in the one place that already owns it
    instead of re-deriving it from settings here and drifting.
    """
    plan: dict[ProviderName, Probe] = {}

    if (brave := services.brave) is not None:
        plan[ProviderName.BRAVE] = _credential_probe(brave.verify_credentials)

    if (exa := services.exa) is not None:
        plan[ProviderName.EXA] = _credential_probe(exa.verify_credentials)

    if (llm := services.llm) is not None:
        plan[ProviderName.LLM] = _credential_probe(llm.verify_credentials)

    if (sec := services.sec) is not None:

        async def probe_sec() -> str:
            # The CIK map is ~1MB, which is more than a probe strictly needs.
            # It is still the right call: the question a SEC probe has to answer
            # is whether this deployment's contact User-Agent is accepted --
            # data.sec.gov answers 403 to anything else, as
            # `test_web_discovery_live` measures -- and this is the one SEC
            # request already in the codebase that asks it. Ingestion fetches
            # the same file on its own schedule, so once more per restart is
            # noise against that rather than new load.
            companies = await sec.company_tickers()
            # Both numbers, because they differ by more than two thousand and
            # reporting only the first read as "SEC lost a quarter of the market".
            symbols = sum(len(entry["tickers"]) for entry in companies.values())
            return f"{len(companies)} companies, {symbols} tickers"

        plan[ProviderName.SEC] = probe_sec

    if (fred := services.fred) is not None:

        async def probe_fred() -> str:
            data = await fred.context(utcnow())
            return f"{len(data)} macro series"

        plan[ProviderName.FRED] = probe_fred

    if (account := services.t212_account) is not None:

        async def probe_trading212() -> str:
            # The account summary, not the metadata endpoints, and the reason is
            # rate limits rather than payload size. `/equity/metadata/exchanges`
            # allows one request per 30 seconds and the instrument sync calls it
            # during the same startup; `ProviderHttpClient`'s token bucket
            # *waits* for a token rather than failing, so a probe there queues
            # behind the sync and blows its own timeout -- measured on
            # 2026-09-06, where it timed out at 10s while the sync held the
            # token. `/equity/account/summary` is one per 5 seconds, on its own
            # bucket, and proves the same credential against the same base URL.
            #
            # A probe must not compete with real work for a scarce token. The
            # instrument sync reports Trading 212's health when it runs anyway;
            # this only has to answer "can we reach the broker at all".
            summary = await account.fetch_account_summary()
            # The currency, never a balance: it is the field the risk engine's
            # currency_alignment rule turns on, and it is not account-sensitive.
            return f"account currency {summary.currency}"

        plan[ProviderName.TRADING212] = probe_trading212

    if (market_data := services.market_data) is not None:

        async def probe_market_data() -> str:
            capability = await market_data.capability(refresh=True)
            if capability.state is not CapabilityState.HEALTHY:
                raise RuntimeError(f"{capability.state.value}: {capability.detail}")
            return f"feed={capability.feed}"

        plan[ProviderName.ALPACA_MARKET_DATA] = probe_market_data

    if services.content_extractor is not None:
        # No credential, no third party, nothing metered: the extractor's
        # dependency is a local library. Reporting that honestly is better than
        # fetching somebody's page on every restart to prove the obvious.
        async def probe_content_extraction() -> str:
            return "local extractor ready; no credential required"

        plan[ProviderName.CONTENT_EXTRACTION] = probe_content_extraction

    return plan


def _credential_probe(verify: Callable[[], Awaitable[None]]) -> Probe:
    """Wrap a ``verify_credentials`` coroutine as a probe with an honest detail."""

    async def probe() -> str:
        await verify()
        return CREDENTIAL_ONLY_DETAIL

    return probe
