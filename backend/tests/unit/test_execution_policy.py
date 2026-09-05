"""Execution policy, and the broker capability gate on automatic authorization.

Two permissions, deliberately on different axes:

* ``ExecutionMode`` and the four live gates decide whether an order may ever be
  *transmitted*. Phase 6 does not touch that truth table, and a test here
  asserts it is unchanged.
* ``ExecutionPolicy`` and the broker's automation capability decide whether a
  proposal may be *authorized* without a human.

Conflating them would let one be granted by satisfying the other.

Trading 212's answer is shaped by its API Terms: clause 4.2(a) prohibits
Algorithmic Trading, and clauses 6.6/6.7 require prior written consent for an
automated customised interface. Live automatic authorization therefore requires
an explicit flag recording that the consent was actually obtained.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stockbrain.broker.automation import automation_capability
from stockbrain.config import ExecutionPolicy, Settings
from stockbrain.enums import Broker


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "test", "stockbrain_secret_key": "k"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _credentialled(**overrides: object) -> Settings:
    return _settings(t212_api_key="key", t212_api_secret="secret", **overrides)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_the_default_policy_is_manual() -> None:
    assert _settings().execution_policy is ExecutionPolicy.MANUAL


def test_automatic_authorization_is_blocked_under_the_manual_policy() -> None:
    settings = _credentialled()
    assert not settings.automatic_authorization_permitted
    assert any("EXECUTION_POLICY is 'MANUAL'" in item for item in settings.automation_blockers)


def test_the_permitted_flag_and_the_blocker_list_can_never_disagree() -> None:
    for settings in (
        _settings(),
        _credentialled(),
        _credentialled(execution_policy="automatic"),
        _credentialled(execution_policy="automatic", proposals_enabled=False),
    ):
        assert settings.automatic_authorization_permitted == (not settings.automation_blockers)


# ---------------------------------------------------------------------------
# Demo: the supported path for exercising automatic authorization
# ---------------------------------------------------------------------------
def test_demo_automatic_authorization_is_permitted_with_credentials() -> None:
    settings = _credentialled(execution_policy="automatic")
    assert settings.t212_env.value == "demo"
    assert settings.automatic_authorization_permitted
    assert settings.automation_blockers == []


def test_demo_automation_needs_credentials_to_read_account_state() -> None:
    settings = _settings(execution_policy="automatic")
    assert not settings.automatic_authorization_permitted
    assert any("credentials are not configured" in item for item in settings.automation_blockers)


def test_disabling_proposals_disables_automation() -> None:
    settings = _credentialled(execution_policy="automatic", proposals_enabled=False)
    assert not settings.automatic_authorization_permitted


# ---------------------------------------------------------------------------
# Live: gated on recorded broker consent
# ---------------------------------------------------------------------------
def test_live_automatic_without_recorded_consent_refuses_to_start() -> None:
    """A deployment that asked for automatic and quietly got manual is one
    nobody is watching, so this is a hard configuration error."""
    with pytest.raises(ValidationError) as excinfo:
        _credentialled(execution_policy="automatic", t212_env="live")
    message = str(excinfo.value)
    assert "T212_AUTOMATED_TRADING_CONSENT_CONFIRMED" in message
    assert "4.2(a)" in message
    assert "T212_ENV=demo" in message, "the message must name the supported alternative"


def test_live_automatic_with_recorded_consent_is_permitted() -> None:
    settings = _credentialled(
        execution_policy="automatic",
        t212_env="live",
        t212_automated_trading_consent_confirmed=True,
    )
    assert settings.automatic_authorization_permitted


def test_the_broker_capability_names_the_terms_clause_it_enforces() -> None:
    """Checked against the *environment*, independently of the policy setting.

    Two layers guard live automation: the configuration refuses to start at all,
    and this capability refuses even if something constructed the settings some
    other way. The second layer is what a future broker adapter will consult.
    """
    capability = automation_capability(_credentialled(t212_env="live"))
    assert not capability.permitted
    assert capability.automation_supported, "the broker supports it; the consent is missing"
    joined = " ".join(capability.blockers)
    assert "4.2(a)" in joined and "written consent" in joined


def test_the_demo_capability_says_why_demo_is_the_recommended_path() -> None:
    capability = automation_capability(_credentialled())
    assert capability.environment == "demo"
    assert "paper" in capability.detail.lower()
    assert capability.permitted


def test_an_unregistered_broker_is_treated_as_forbidding_automation() -> None:
    """Fail closed: a broker whose policy nobody wrote down has not granted one."""

    class Unknown:
        value = "FUTURE_BROKER"

    capability = automation_capability(_credentialled(), Unknown())  # type: ignore[arg-type]
    assert not capability.automation_supported
    assert not capability.permitted


def test_each_broker_advertises_its_own_answer() -> None:
    from stockbrain.broker.automation import _CAPABILITIES

    assert set(_CAPABILITIES) == {Broker.TRADING212}


# ---------------------------------------------------------------------------
# The live *execution* truth table is untouched by this phase
# ---------------------------------------------------------------------------
def test_automatic_authorization_does_not_grant_live_execution() -> None:
    """Authorizing and transmitting are separate permissions with separate gates."""
    settings = _credentialled(
        execution_policy="automatic",
        t212_env="live",
        t212_automated_trading_consent_confirmed=True,
    )
    assert settings.automatic_authorization_permitted
    assert not settings.live_execution_permitted
    assert settings.execution_blockers


def test_the_automation_consent_flag_is_not_one_of_the_four_execution_gates() -> None:
    """It must not accidentally satisfy the live-execution check."""
    with_consent = _credentialled(t212_automated_trading_consent_confirmed=True)
    without = _credentialled()
    assert with_consent.execution_blockers == without.execution_blockers


def test_live_execution_still_needs_all_four_original_gates() -> None:
    settings = _credentialled(
        t212_env="live",
        t212_live_execution_enabled=True,
        t212_written_consent_confirmed=True,
        execution_mode="manual_approval",
    )
    assert settings.live_execution_permitted
    assert settings.execution_blockers == []
