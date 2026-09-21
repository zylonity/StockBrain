"""The new disclosure-feed settings: defaults off and CSV parsing."""

from __future__ import annotations

from typing import Any

import pytest

from stockbrain.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def test_every_disclosure_feed_ships_disabled() -> None:
    settings = _settings()
    assert settings.disclosure_feeds_enabled is False
    assert settings.investegate_enabled is False
    assert settings.eqs_enabled is False
    assert settings.cnmv_enabled is False
    assert settings.globenewswire_enabled is False
    assert settings.actusnews_enabled is False


def test_disclosure_feed_defaults_match_the_spec() -> None:
    settings = _settings()
    assert settings.disclosure_feed_timeout_seconds == 20.0
    assert settings.investegate_interval_seconds == 300.0
    assert settings.investegate_max_pages == 3
    assert settings.eqs_interval_seconds == 300.0
    assert settings.cnmv_interval_seconds == 600.0
    assert settings.globenewswire_interval_seconds == 900.0
    assert settings.actusnews_interval_seconds == 900.0
    assert settings.globenewswire_countries == [
        "France",
        "Netherlands",
        "Belgium",
        "Portugal",
        "Spain",
        "Canada",
    ]


def test_globenewswire_countries_accepts_a_comma_separated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOBENEWSWIRE_COUNTRIES", "France,Canada")
    assert _settings().globenewswire_countries == ["France", "Canada"]
