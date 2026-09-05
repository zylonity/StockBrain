"""Phase 9 live provider verification.  Opt-in, read-only, minimal.

    pytest -m live -s tests/integration/test_phase9_live.py

Every test here makes **read-only** calls.  There is deliberately no live
Firecrawl test: Firecrawl bills for every request its infrastructure processes,
the Phase 2 incident emptied the allowance, and a test suite that spends money
on each run is a test suite nobody runs.  The Firecrawl work is verified against
``firecrawl_activity_logs.csv``, the PostgreSQL job history and mocks -- see
``tests/unit/test_firecrawl_budget.py`` and
``tests/integration/test_firecrawl_scheduler.py``.

What the FX tests print is the *contract*: the endpoint, the entitlement, the
response shape and the rate's age.  Never a balance, never a position, never a
key.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.config import Settings
from stockbrain.fx.alpaca import AlpacaFxProvider
from stockbrain.fx.base import FxRateGrade
from stockbrain.fx.frankfurter import FrankfurterFxProvider
from stockbrain.fx.service import FxService

pytestmark = pytest.mark.live

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _live_settings(**overrides: object) -> Settings:
    """Settings from the repository ``.env``.

    Phase 4's bug 11: a live test that reads only ``os.environ`` can never run,
    because the credentials live in the git-ignored ``.env`` and nothing exports
    them. ``_env_file`` is passed explicitly for that reason.
    """
    base: dict[str, object] = {"app_env": "local", "log_level": "WARNING"}
    base.update(overrides)
    if _ENV_PATH.is_file():
        return Settings(_env_file=str(_ENV_PATH), **base)  # type: ignore[arg-type]
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Alpaca forex: entitlement
# ---------------------------------------------------------------------------
async def test_alpaca_forex_entitlement_is_measured_not_assumed() -> None:
    """Does the plan that covers IEX equities also cover forex?

    Measured 2026-09-05: **no.** ``GET /v1beta1/forex/latest/rates`` answers
    HTTP 403 ``{"message":"forbidden: insufficient grants"}`` on this account.
    That is a fact about the subscription, not about the credential, and it is
    why ``FxRateGrade`` and a second provider exist at all.

    The test passes either way -- it records what the account can do rather
    than asserting a particular answer -- because the point is the measurement.
    """
    settings = _live_settings()
    if not (
        settings.alpaca_api_key.get_secret_value() and settings.alpaca_api_secret.get_secret_value()
    ):
        pytest.skip("ALPACA_API_KEY / ALPACA_API_SECRET are not configured")

    provider = AlpacaFxProvider(settings)
    try:
        capability = await provider.capability()
        print("\n--- Alpaca forex ---")
        print("endpoint          /v1beta1/forex/latest/rates")
        print(
            f"probe pair        {settings.fx_probe_base_currency}{settings.fx_probe_quote_currency}"
        )
        print(f"grade             {capability.grade.value if capability.grade else '-'}")
        print(f"available         {capability.available}")
        for blocker in capability.blockers:
            print(f"blocker           {blocker}")
        if capability.detail:
            print(f"detail            {capability.detail}")

        if capability.available:
            rate = await provider.latest(
                settings.fx_probe_base_currency, settings.fx_probe_quote_currency
            )
            # A rate, and nothing about the account.
            print(f"pair              {rate.pair}")
            print(f"rate_type         {rate.rate_type}")
            print(f"timestamp         {rate.provider_timestamp.isoformat()}")
            print(f"precision         {rate.provider_timestamp_precision}")
            print(f"two-sided         {rate.bid is not None and rate.ask is not None}")
            assert rate.rate > 0
            assert rate.grade is FxRateGrade.EXECUTION
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# Frankfurter: the reference-grade fallback
# ---------------------------------------------------------------------------
async def test_frankfurter_publishes_the_pair_and_reports_its_grade() -> None:
    """No key, no quota, and explicitly not a dealing rate.

    Verifies the v2 response shape -- a JSON **array** of pair rows, not the v1
    ``{"base", "rates"}`` object -- and that the date-only publication is aged
    conservatively from the start of the published day.
    """
    settings = _live_settings(fx_provider="frankfurter", fx_allow_reference_grade=True)
    provider = FrankfurterFxProvider(settings)
    try:
        rate = await provider.latest(
            settings.fx_probe_base_currency, settings.fx_probe_quote_currency
        )
        from stockbrain.db.base import utcnow

        age = rate.age_seconds(utcnow())
        print("\n--- Frankfurter ---")
        print("endpoint          /v2/rates")
        print(f"pair              {rate.pair}")
        print(f"rate_type         {rate.rate_type}")
        print(f"grade             {rate.grade.value}")
        print(f"published         {rate.provider_timestamp.date().isoformat()}")
        print(f"precision         {rate.provider_timestamp_precision}")
        print(f"age (s)           {age}")
        print(
            f"within reference limit ({settings.fx_reference_max_age_seconds:g}s): "
            f"{age <= Decimal(str(settings.fx_reference_max_age_seconds))}"
        )

        assert rate.rate > 0
        assert rate.grade is FxRateGrade.REFERENCE
        assert rate.provider_timestamp_precision == "day"
        # Aged from midnight UTC on the published date, which over-states the
        # age. Under-stating it is the one direction that would matter.
        assert rate.provider_timestamp.hour == 0
    finally:
        await provider.aclose()


async def test_a_reference_rate_is_refused_without_the_explicit_opt_in() -> None:
    """The provider works and is deliberately not trusted to size a trade.

    Frankfurter's own documentation says it "is not for live trading". This
    asserts StockBrain honours that by default rather than by intention.
    """
    settings = _live_settings(fx_provider="none")
    service = FxService(
        settings.model_copy(update={"fx_allow_reference_grade": False}),
        provider=FrankfurterFxProvider(settings),
    )
    try:
        resolution = await service.resolve(from_currency="GBP", to_currency="USD")
        print("\n--- Reference-grade refusal ---")
        print(f"usable            {resolution.usable}")
        for blocker in resolution.blockers:
            print(f"blocker           {blocker}")
        assert not resolution.usable
        assert any("reference-grade" in blocker for blocker in resolution.blockers)
    finally:
        await service.aclose()


async def test_the_full_fx_path_produces_a_usable_conversion_when_permitted() -> None:
    """End to end: fetch, judge, convert.

    The arithmetic the GBP account actually needs -- a 500 GBP cap expressed in
    USD -- with the rate's provenance attached. No account data is read and no
    order is contemplated.
    """
    settings = _live_settings(
        fx_provider="frankfurter",
        fx_allow_reference_grade=True,
        risk_require_same_currency=False,
    )
    service = FxService(settings, provider=FrankfurterFxProvider(settings))
    try:
        resolution = await service.resolve(from_currency="GBP", to_currency="USD")
        print("\n--- FX conversion ---")
        print(f"provider          {resolution.provider}")
        print(f"usable            {resolution.usable}")
        for blocker in resolution.blockers:
            print(f"blocker           {blocker}")
        if not resolution.usable:
            pytest.skip(f"FX unusable right now: {'; '.join(resolution.blockers)}")

        assert resolution.rate is not None
        conversion = resolution.rate.convert(
            Decimal("500"), "GBP", "USD", now=resolution.resolved_at
        )
        print(f"direction         {conversion.direction.value}")
        print(f"500 GBP           -> {conversion.converted} USD")
        back = resolution.rate.convert(
            conversion.converted, "USD", "GBP", now=resolution.resolved_at
        )
        print(f"round trip        -> {back.converted} GBP")
        assert back.converted == Decimal("500")
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# Firecrawl: deliberately not called
# ---------------------------------------------------------------------------
def test_no_live_firecrawl_call_is_made_by_this_suite() -> None:
    """An assertion about the test suite itself.

    Firecrawl charges for every request its infrastructure processes. A test
    that calls it spends the operator's allowance on every run, and the Phase 2
    incident is what that costs. The budget, the cadence and the two-stage model
    are verified against recorded evidence and mocks instead.

    Stated as a test so that adding one is a deliberate act with a failing
    assertion attached, not an oversight.
    """
    live_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(live_dir.glob("test_*live*.py")):
        source = path.read_text()
        for needle in ("FirecrawlClient(", ".search(", "/v2/search", "/v2/scrape"):
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], "a live test appears to call Firecrawl: " + "; ".join(offenders)
    # And the environment this repository ships with keeps it off.
    assert os.environ.get("FIRECRAWL_LIVE_SEARCH") in (None, "", "no")
