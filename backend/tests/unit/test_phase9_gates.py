"""The complete gate truth table, including everything Phase 9 added.

Phase 6 established the four live-execution gates and an exhaustive test that
exactly one of sixteen combinations permits live transmission.  Phase 9 added
three more independent switches — the pending-order limit, the FX policy pair,
and web authentication — and the property that has to hold across all of them
is the same one: **no default and no omission may enable something dangerous.**

So this file asserts the shape of the whole configuration surface rather than
just the live gates: what a bare ``Settings()`` permits, which pairings are
refused at startup, and which switches are deliberately *not* gates for each
other.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stockbrain.api.auth import hash_password, web_auth_blockers
from stockbrain.config import BrokerEnvironment, ExecutionMode, FxProviderName, Settings


def _settings(**overrides: object) -> Settings:
    """Settings built from *nothing but* the overrides.

    ``_env_file=None`` matters: Phase 8's bug 20 was a test that passed only
    while a variable happened to be absent from the developer's ``.env``.
    """
    base: dict[str, object] = {"app_env": "test", "_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The default posture
# ---------------------------------------------------------------------------
def test_a_bare_default_deployment_is_safe_in_every_respect() -> None:
    """The single most important test in this file.

    "Fresh/default deployment must remain non-live" restated across every axis
    Phase 9 touched. Each of these is a separate way a default could have been
    wrong, and all of them are checked in one place so a new setting has to be
    considered here.
    """
    settings = _settings()

    # Broker: demo, nothing transmits, no live gate satisfied.
    assert settings.t212_env is BrokerEnvironment.DEMO
    assert settings.t212_live_execution_enabled is False
    assert settings.t212_written_consent_confirmed is False
    assert settings.t212_automated_trading_consent_confirmed is False
    assert settings.t212_execution_enabled is False
    assert settings.live_execution_permitted is False
    assert settings.order_transmission_permitted is False
    assert settings.execution_mode is ExecutionMode.MANUAL_APPROVAL

    # Cost: the one provider that can spend money on a schedule is off.
    assert settings.firecrawl_enabled is False
    assert settings.firecrawl_available is False

    # Currency: no rate source, and same-currency required.
    assert settings.fx_provider is FxProviderName.NONE
    assert settings.risk_require_same_currency is True
    assert settings.fx_allow_reference_grade is False

    # Access: authentication on, and unusable until a password is set --
    # which means nothing is reachable.
    assert settings.web_auth_enabled is True
    assert web_auth_blockers(settings) != []

    # The broker's documented pending-order limit, with headroom.
    assert settings.t212_max_pending_orders_per_ticker == 50
    assert settings.t212_pending_order_headroom > 0


def test_the_pending_order_ceiling_leaves_headroom_below_the_documented_limit() -> None:
    """StockBrain never discovers the limit by hitting it.

    The limit is enforced by a rejection from a *non-idempotent* endpoint, the
    broker's pending list can lag a fill, and the operator can queue an order by
    hand between one check and the next.
    """
    settings = _settings()
    ceiling = settings.t212_max_pending_orders_per_ticker - settings.t212_pending_order_headroom
    assert 0 < ceiling < settings.t212_max_pending_orders_per_ticker


def test_headroom_that_swallows_the_whole_limit_is_refused() -> None:
    """Otherwise execution would silently do nothing rather than error."""
    with pytest.raises(ValidationError, match="T212_PENDING_ORDER_HEADROOM"):
        _settings(t212_max_pending_orders_per_ticker=5, t212_pending_order_headroom=5)


# ---------------------------------------------------------------------------
# The live gates, unchanged by Phase 9
# ---------------------------------------------------------------------------
def test_the_four_live_gates_still_admit_exactly_one_combination() -> None:
    """Phase 9 added switches beside these, never among them.

    Re-asserted here rather than relying on the Phase 6 test, because the point
    is that a phase which added an FX policy, a password and a pending-order
    limit did not touch this table.
    """
    permitted = 0
    for env, enabled, consent, mode in itertools.product(
        ["demo", "live"], [True, False], [True, False], ["manual_approval", "research_only"]
    ):
        try:
            settings = _settings(
                t212_env=env,
                t212_live_execution_enabled=enabled,
                t212_written_consent_confirmed=consent,
                execution_mode=mode,
                t212_api_key="key",
                t212_api_secret="secret",
            )
        except ValidationError:
            # A contradictory combination refuses to start, which is a stronger
            # answer than "not permitted".
            continue
        if settings.live_execution_permitted:
            permitted += 1
            assert (env, enabled, consent, mode) == (
                "live",
                True,
                True,
                "manual_approval",
            )
    assert permitted == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"fx_provider": "frankfurter", "fx_allow_reference_grade": True},
        {
            "risk_require_same_currency": False,
            "fx_provider": "frankfurter",
            "fx_allow_reference_grade": True,
        },
        {"web_auth_enabled": False, "web_trusted_network_acknowledged": True},
        {"firecrawl_enabled": True, "firecrawl_api_key": "fc-x"},
        {"t212_max_pending_orders_per_ticker": 500},
        {"alerts_enabled": False},
    ],
)
def test_no_phase_9_setting_can_enable_live_execution(extra: dict[str, object]) -> None:
    """None of the new switches is a live gate, and none loosens one.

    Each of these is a real, supported configuration; none of them may make a
    demo deployment able to transmit live.
    """
    settings = _settings(t212_api_key="key", t212_api_secret="secret", **extra)
    assert settings.live_execution_permitted is False
    assert settings.order_transmission_permitted is False


def test_the_automation_consent_flag_is_still_not_a_live_gate() -> None:
    """Authorizing and transmitting are separate permissions.

    Restated because Phase 9 touched the authorization path (FX) and the
    transmission path (pending orders) in the same phase.
    """
    settings = _settings(
        t212_automated_trading_consent_confirmed=True,
        t212_api_key="key",
        t212_api_secret="secret",
    )
    assert settings.live_execution_permitted is False
    assert not any("AUTOMATED_TRADING" in blocker for blocker in settings.execution_blockers)


# ---------------------------------------------------------------------------
# The FX policy pair
# ---------------------------------------------------------------------------
def test_permitting_cross_currency_without_a_rate_source_is_refused() -> None:
    """Bug 22, as a startup failure.

    Through Phase 8 this produced a ``WARN`` and a quantity computed by dividing
    an account-currency cap by an instrument-currency price. The refusal names
    the arithmetic, so the message teaches rather than just blocks.
    """
    with pytest.raises(ValidationError, match="RISK_REQUIRE_SAME_CURRENCY=false"):
        _settings(risk_require_same_currency=False, fx_provider="none")


def test_a_reference_grade_provider_without_the_opt_in_is_refused() -> None:
    """Every rate it returned would be blocked, which would present as
    "cross-currency silently does nothing"."""
    with pytest.raises(ValidationError, match="FX_ALLOW_REFERENCE_GRADE"):
        _settings(fx_provider="frankfurter")


def test_both_fx_switches_together_are_accepted() -> None:
    settings = _settings(
        risk_require_same_currency=False,
        fx_provider="frankfurter",
        fx_allow_reference_grade=True,
    )
    assert settings.fx_provider is FxProviderName.FRANKFURTER
    assert settings.fx_blockers == []


def test_a_reference_freshness_limit_below_the_execution_one_is_refused() -> None:
    """A daily fixing cannot be held to a stricter bar than a live quote.

    Inverted, the reference budget would be the binding one and the grade
    distinction would mean the opposite of what it says.
    """
    with pytest.raises(ValidationError, match="FX_REFERENCE_MAX_AGE_SECONDS"):
        _settings(
            fx_max_age_seconds=3600.0,
            fx_reference_max_age_seconds=900.0,
        )


def test_a_meaningless_fx_drift_envelope_is_refused() -> None:
    for value in (Decimal("0"), Decimal("-0.01"), Decimal("1.5")):
        with pytest.raises(ValidationError, match="FX_MAX_RATE_DRIFT_PCT"):
            _settings(fx_max_rate_drift_pct=value)


def test_a_same_currency_probe_pair_is_refused() -> None:
    """``GBPGBP`` is not a rate, and probing for it would report a permanent
    failure that looks like an outage."""
    with pytest.raises(ValidationError, match="must differ"):
        _settings(fx_probe_base_currency="GBP", fx_probe_quote_currency="GBP")


def test_a_malformed_currency_code_is_refused() -> None:
    for bad in ("US", "USDD", "12A", ""):
        with pytest.raises(ValidationError, match="ISO 4217"):
            _settings(fx_probe_base_currency=bad)


# ---------------------------------------------------------------------------
# Web authentication
# ---------------------------------------------------------------------------
def test_production_without_authentication_needs_the_acknowledgement() -> None:
    """Specification section 19: "If accessed only over LAN/Tailscale, still
    require auth."

    Running behind an authenticating reverse proxy is supported. Never having
    set a password is not, and the difference has to be something the operator
    states rather than something the code infers.
    """
    with pytest.raises(ValidationError, match="WEB_TRUSTED_NETWORK_ACKNOWLEDGED"):
        _settings(
            app_env="production",
            stockbrain_secret_key="a-key",
            web_auth_enabled=False,
        )
    assert (
        _settings(
            app_env="production",
            stockbrain_secret_key="a-key",
            web_auth_enabled=False,
            web_trusted_network_acknowledged=True,
        ).web_auth_enabled
        is False
    )


def test_a_missing_password_does_not_stop_the_process() -> None:
    """It stops every protected route instead.

    Refusing to start would leave the operator with no way to read the reason;
    a container that comes up, serves its probes and grants nothing is the
    better failure.
    """
    settings = _settings(stockbrain_secret_key="a-key")
    assert settings.web_auth_enabled is True
    assert any("WEB_OWNER_PASSWORD_HASH" in blocker for blocker in web_auth_blockers(settings))


def test_a_fully_configured_deployment_is_protected() -> None:
    settings = _settings(
        stockbrain_secret_key="a-key",
        web_owner_password_hash=hash_password("a-long-enough-password"),
    )
    assert web_auth_blockers(settings) == []


def test_production_still_requires_a_signing_key() -> None:
    """Unchanged from Phase 1, and now load-bearing for sessions too."""
    with pytest.raises(ValidationError, match="STOCKBRAIN_SECRET_KEY"):
        _settings(app_env="production")


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------
def test_firecrawl_needs_a_key_and_a_switch_and_a_budget() -> None:
    """Three independent conditions, each reported by name.

    "Available" is "no blockers remain", so the flag and the reason list can
    never disagree.
    """
    assert _settings().firecrawl_available is False
    assert _settings(firecrawl_enabled=True).firecrawl_available is False
    assert _settings(firecrawl_api_key="fc-x").firecrawl_available is False
    assert _settings(firecrawl_enabled=True, firecrawl_api_key="fc-x").firecrawl_available is True
    # A zero cap is a deliberate off switch and is reported as a blocker rather
    # than as a working provider that happens to refuse everything.
    assert (
        _settings(
            firecrawl_enabled=True,
            firecrawl_api_key="fc-x",
            firecrawl_max_searches_per_day=0,
        ).firecrawl_available
        is False
    )


def test_discovery_disabled_disables_firecrawl_too() -> None:
    """The subsystem switch is above the provider switch."""
    settings = _settings(discovery_enabled=False, firecrawl_enabled=True, firecrawl_api_key="fc-x")
    assert settings.firecrawl_available is False
    assert any("DISCOVERY_ENABLED" in blocker for blocker in settings.firecrawl_blockers)


def test_an_unknown_search_source_is_refused_at_startup() -> None:
    """The API would reject it after processing -- and billing -- the request."""
    with pytest.raises(ValidationError, match="documents only"):
        _settings(firecrawl_search_sources="web,podcasts")


# ---------------------------------------------------------------------------
# The predicates cannot disagree with their reasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"t212_api_key": "key", "t212_api_secret": "secret"},
        {"t212_execution_enabled": True},
        {"firecrawl_enabled": True, "firecrawl_api_key": "fc-x"},
        {"fx_provider": "frankfurter", "fx_allow_reference_grade": True},
        {"web_auth_enabled": False},
        {
            "t212_env": "live",
            "t212_live_execution_enabled": True,
            "t212_written_consent_confirmed": True,
            "t212_api_key": "key",
            "t212_api_secret": "secret",
        },
    ],
)
def test_every_permission_predicate_equals_the_absence_of_its_blockers(
    overrides: dict[str, object],
) -> None:
    """The pattern Phase 1's bug 2 established, extended to the new predicates.

    A banner that lists a blocker beside a green light is worse than either
    answer alone, so each predicate is *defined* as "no blockers remain" rather
    than computed separately.
    """
    settings = _settings(**overrides)
    assert settings.live_execution_permitted == (not settings.execution_blockers)
    assert settings.order_transmission_permitted == (not settings.order_transmission_blockers)
    assert settings.automatic_authorization_permitted == (not settings.automation_blockers)
    assert settings.telegram_available == (not settings.telegram_blockers)
    assert settings.firecrawl_available == (not settings.firecrawl_blockers)
    assert settings.fx_configured == (not settings.fx_blockers)


def test_no_blocker_message_contains_a_secret_value() -> None:
    """Blockers are rendered in the GUI and in the startup log.

    They name *settings*, never values -- which is why they read
    "T212_WRITTEN_CONSENT_CONFIRMED is false" rather than echoing anything.
    """
    settings = _settings(
        t212_api_key="a-secret-broker-key",
        t212_api_secret="a-secret-broker-secret",
        firecrawl_api_key="fc-a-secret-key",
        stockbrain_secret_key="a-secret-signing-key",
        web_owner_password_hash=hash_password("a-secret-password"),
        telegram_bot_token="123:a-secret-token",
    )
    everything = " ".join(
        [
            *settings.execution_blockers,
            *settings.order_transmission_blockers,
            *settings.automation_blockers,
            *settings.telegram_blockers,
            *settings.firecrawl_blockers,
            *settings.fx_blockers,
            *web_auth_blockers(settings),
        ]
    )
    for secret in (
        "a-secret-broker-key",
        "a-secret-broker-secret",
        "fc-a-secret-key",
        "a-secret-signing-key",
        "a-secret-password",
        "a-secret-token",
        "scrypt$",
    ):
        assert secret not in everything, secret
