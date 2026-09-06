"""The startup provider sweep.

The subject here is the *orchestration*, not any individual provider: that a
probe which raises cannot take down a container, that a probe which hangs
cannot stall one, that a disabled provider is never asked, and that what the
board ends up showing is what the probes actually measured.

Each provider's own probe is tested where that provider lives -- the Brave and
Exa credential contracts in ``test_web_discovery_providers.py``, against the
bodies their live APIs really returned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from stockbrain.enums import ProviderStatus
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.observability.probes import (
    CREDENTIAL_ONLY_DETAIL,
    ProbeOutcome,
    build_probe_plan,
    run_probe,
)
from stockbrain.startup import probe_enabled_providers


# ---------------------------------------------------------------------------
# run_probe: turning anything a provider does into an outcome
# ---------------------------------------------------------------------------
async def test_a_successful_probe_records_healthy_with_its_own_detail() -> None:
    async def probe() -> str:
        return "17 exchanges"

    outcome = await run_probe(ProviderName.TRADING212, probe)

    assert outcome == ProbeOutcome(
        ProviderName.TRADING212, ProviderStatus.HEALTHY, detail="17 exchanges"
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # A credential or entitlement fault is an operator's problem: nothing
        # improves by waiting, so it must not look like a transient blip.
        (ProviderAuthError("brave: subscription token rejected"), ProviderStatus.DOWN),
        (ProviderEntitlementError("alpaca: plan does not cover sip"), ProviderStatus.DOWN),
        # These are things that plausibly fix themselves.
        (ProviderRateLimited("exa: rate limited"), ProviderStatus.DEGRADED),
        (ProviderUnavailable("sec: HTTP 503"), ProviderStatus.DEGRADED),
        (RuntimeError("something unforeseen"), ProviderStatus.DEGRADED),
    ],
)
async def test_a_failing_probe_is_classified_never_raised(
    error: Exception, expected: ProviderStatus
) -> None:
    """The property that keeps a third party from breaking a boot."""

    async def probe() -> str:
        raise error

    outcome = await run_probe(ProviderName.BRAVE, probe)

    assert outcome.status is expected
    assert outcome.detail  # an operator gets told what happened


async def test_a_hanging_probe_is_cut_off_rather_than_stalling_startup() -> None:
    """A provider that accepts the connection and then says nothing.

    Without the timeout this is the failure that leaves a container in
    "starting" until Docker's healthcheck grace period runs out.
    """
    started = asyncio.Event()

    async def probe() -> str:
        started.set()
        await asyncio.sleep(30)
        return "never"

    outcome = await run_probe(ProviderName.EXA, probe, timeout_seconds=0.05)

    assert started.is_set(), "the probe should have been entered"
    assert outcome.status is ProviderStatus.DEGRADED
    assert "0.05s" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# build_probe_plan: only what this deployment actually configured
# ---------------------------------------------------------------------------
@dataclass
class _FakeServices:
    """Stands in for ``ServiceContainer``'s optional client attributes.

    Every client on the real container is ``None`` until its configuration gate
    opens, which is exactly the signal ``build_probe_plan`` reads; a fake that
    reproduces that shape tests the real rule.
    """

    brave: Any = None
    exa: Any = None
    deepseek: Any = None
    sec: Any = None
    fred: Any = None
    t212_account: Any = None
    market_data: Any = None
    content_extractor: Any = None


class _StubVerifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def verify_credentials(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def test_a_deployment_with_nothing_configured_probes_nothing() -> None:
    """No clients means no probes -- not a plan full of failing ones."""
    assert build_probe_plan(_FakeServices()) == {}  # type: ignore[arg-type]


def test_only_configured_providers_appear_in_the_plan() -> None:
    """A disabled provider is never probed.

    This is the "every *enabled* component" rule. It holds without a second
    list of enabled providers, because a client that was never constructed is
    already the answer.
    """
    services = _FakeServices(brave=_StubVerifier(), deepseek=_StubVerifier())

    plan = build_probe_plan(services)  # type: ignore[arg-type]

    assert set(plan) == {ProviderName.BRAVE, ProviderName.DEEPSEEK}
    assert ProviderName.EXA not in plan
    assert ProviderName.FIRECRAWL not in plan


async def test_a_credential_probe_says_that_is_what_it_checked() -> None:
    """The detail must not imply a search happened when one did not."""
    brave = _StubVerifier()
    plan = build_probe_plan(_FakeServices(brave=brave))  # type: ignore[arg-type]

    outcome = await run_probe(ProviderName.BRAVE, plan[ProviderName.BRAVE])

    assert brave.calls == 1
    assert outcome.status is ProviderStatus.HEALTHY
    assert outcome.detail == CREDENTIAL_ONLY_DETAIL
    assert "no billable request" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# probe_enabled_providers: what the board ends up showing
# ---------------------------------------------------------------------------
async def test_the_sweep_records_every_result_onto_the_registry() -> None:
    registry = ProviderHealthRegistry()
    services = _FakeServices(
        brave=_StubVerifier(),
        exa=_StubVerifier(ProviderAuthError("exa: authentication rejected (HTTP 401)")),
    )

    statuses = await probe_enabled_providers(services, registry)  # type: ignore[arg-type]

    assert statuses == {
        ProviderName.BRAVE: ProviderStatus.HEALTHY,
        ProviderName.EXA: ProviderStatus.DOWN,
    }
    assert registry.get(ProviderName.BRAVE).status is ProviderStatus.HEALTHY
    assert registry.get(ProviderName.BRAVE).last_ok_at is not None
    assert registry.get(ProviderName.EXA).status is ProviderStatus.DOWN
    assert "401" in (registry.get(ProviderName.EXA).detail or "")


async def test_one_broken_provider_does_not_stop_the_others_being_probed() -> None:
    """Concurrency must not become "the first failure wins".

    ``asyncio.gather`` without ``return_exceptions`` would cancel the siblings
    of a raising task -- which is safe here only because ``run_probe`` never
    raises, and this is the test that keeps it that way.
    """
    good = _StubVerifier()
    bad = _StubVerifier(ProviderAuthError("brave: subscription token rejected (HTTP 422)"))
    registry = ProviderHealthRegistry()

    await probe_enabled_providers(
        _FakeServices(brave=bad, exa=good, deepseek=good),  # type: ignore[arg-type]
        registry,
    )

    assert bad.calls == 1
    assert good.calls == 2, "the healthy providers were still asked"
    assert registry.get(ProviderName.BRAVE).status is ProviderStatus.DOWN
    assert registry.get(ProviderName.EXA).status is ProviderStatus.HEALTHY
    assert registry.get(ProviderName.DEEPSEEK).status is ProviderStatus.HEALTHY


async def test_a_sweep_with_nothing_to_probe_is_a_no_op() -> None:
    registry = ProviderHealthRegistry()

    assert await probe_enabled_providers(_FakeServices(), registry) == {}  # type: ignore[arg-type]


async def test_probes_run_concurrently_rather_than_one_after_another() -> None:
    """Startup cost is the slowest probe, not the sum of them.

    Asserted through observed concurrency rather than wall-clock timing, so the
    test does not become flaky on a loaded machine.
    """
    live = 0
    peak = 0

    class _Slow:
        async def verify_credentials(self) -> None:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await probe_enabled_providers(
        _FakeServices(brave=_Slow(), exa=_Slow(), deepseek=_Slow()),  # type: ignore[arg-type]
        ProviderHealthRegistry(),
    )

    assert peak == 3, f"probes ran with a peak concurrency of {peak}, expected all three at once"


async def test_a_healthy_sweep_clears_the_degraded_reading_a_restart_causes() -> None:
    """The outcome the whole change exists for.

    ``overall_status`` degrades on any ``UNKNOWN``, so a freshly booted
    container used to report ``DEGRADED`` no matter how healthy it was. Probing
    replaces those unknowns with measurements.
    """
    registry = ProviderHealthRegistry()
    for name in ProviderName:
        registry.set_disabled(name, "not configured in this test")
    registry.record(ProviderName.POSTGRES, ProviderStatus.HEALTHY)
    assert registry.overall_status() is ProviderStatus.HEALTHY

    registry.record(ProviderName.BRAVE, ProviderStatus.UNKNOWN)
    assert registry.overall_status() is ProviderStatus.DEGRADED

    await probe_enabled_providers(
        _FakeServices(brave=_StubVerifier()),  # type: ignore[arg-type]
        registry,
    )

    assert registry.get(ProviderName.BRAVE).status is ProviderStatus.HEALTHY
    assert registry.overall_status() is ProviderStatus.HEALTHY
