"""The configuration catalogue: complete, honest, and never a secret leak.

This is the page an operator reads before changing something that can move
money, so the tests here are about the two ways it could lie: by naming an
attribute that no longer exists (a blank row where a limit should be), and by
rendering a value it must never render.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from stockbrain.api.settings_model import (
    CATALOGUE,
    Mutability,
    SettingSpec,
    build_settings_view,
    catalogue_attributes,
)
from stockbrain.config import Settings

_SETTINGS = Settings(
    app_env="test",
    stockbrain_secret_key="unit-test-signing-key",
    telegram_bot_token="1234:super-secret-bot-token",
    t212_api_key="t212-secret-key",
    web_owner_password_hash="scrypt$1$2$3$deadbeef",
)

_RUNTIME_VALUES = {
    "trading_halted": "running",
    "discovery_hold": "running",
    "notification_preferences": "see the categories below",
}


def _specs() -> list[SettingSpec]:
    return [spec for group in CATALOGUE for spec in group.specs]


def test_every_catalogued_attribute_exists_on_settings_or_is_runtime_state() -> None:
    """A renamed configuration field must break a test, not a page.

    The failure mode this prevents is silent: a spec whose attribute vanished
    renders as an empty value, and an operator reading "Maximum trade notional:
    (blank)" has no way to tell that from a limit that is genuinely unset.
    """
    missing = [
        spec.attr
        for spec in _specs()
        if spec.attr not in _RUNTIME_VALUES and not hasattr(_SETTINGS, spec.attr)
    ]
    assert missing == []


def test_no_secret_value_is_ever_rendered() -> None:
    revealed = {
        "1234:super-secret-bot-token",
        "t212-secret-key",
        "unit-test-signing-key",
        "scrypt$1$2$3$deadbeef",
    }
    groups = build_settings_view(_SETTINGS, runtime_values=_RUNTIME_VALUES)
    for group in groups:
        for item in group.settings:
            assert item.value not in revealed
            if item.mutability is Mutability.SECRET:
                assert item.value is None
                assert item.configured is not None


def test_a_configured_secret_reports_only_that_it_is_configured() -> None:
    groups = {group.key: group for group in build_settings_view(_SETTINGS)}
    telegram = {item.key: item for item in groups["telegram"].settings}
    assert telegram["telegram_bot_token"].configured is True
    assert telegram["telegram_bot_token"].value is None

    market = {item.key: item for item in groups["market_data"].settings}
    # Nothing configured this one, and saying so is the useful answer.
    assert market["alpaca_api_key"].configured is False


def test_a_secretstr_can_never_be_rendered_even_if_miscatalogued() -> None:
    """The renderer refuses structurally, not by convention.

    A spec that named a secret field but forgot ``SECRET`` would otherwise
    print it. Belt and braces, because the cost of being wrong here is a
    credential in a browser tab.
    """
    from stockbrain.api.settings_model import _render

    assert _render(SecretStr("hunter2-hunter2")) is None


def test_the_execution_gates_are_never_runtime_editable() -> None:
    """The four live gates exist so that enabling live execution needs a restart.

    A ``RUNTIME`` mutability on any of them would tell the GUI to render a
    switch, and a switch is exactly what these must not have.
    """
    gates = {
        "t212_env",
        "t212_execution_enabled",
        "t212_live_execution_enabled",
        "t212_written_consent_confirmed",
        "t212_automated_trading_consent_confirmed",
        "execution_mode",
        "execution_policy",
    }
    for spec in _specs():
        if spec.attr in gates:
            assert spec.mutability is Mutability.RESTART_REQUIRED, spec.attr


def test_no_risk_limit_is_runtime_editable() -> None:
    """A limit that could change mid-evaluation is a limit two halves of one
    decision could disagree about."""
    for spec in _specs():
        if spec.attr.startswith("risk_"):
            assert spec.mutability is Mutability.RESTART_REQUIRED, spec.attr


def test_every_runtime_row_names_the_control_that_changes_it() -> None:
    for spec in _specs():
        if spec.mutability is Mutability.RUNTIME:
            assert spec.control, spec.attr


def test_group_blockers_are_reported_where_a_group_has_them() -> None:
    groups = {group.key: group for group in build_settings_view(_SETTINGS)}
    # Nothing configured Telegram in this fixture, so the group must say why
    # rather than rendering a set of switches that could never fire.
    assert groups["telegram"].blockers
    assert groups["execution"].blockers
    assert groups["execution"].warning


@pytest.mark.parametrize("attribute", sorted(set(catalogue_attributes())))
def test_each_attribute_renders_without_raising(attribute: str) -> None:
    groups = build_settings_view(_SETTINGS, runtime_values=_RUNTIME_VALUES)
    rendered = {item.key for group in groups for item in group.settings}
    assert attribute in rendered
