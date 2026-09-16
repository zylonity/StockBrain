"""Centralised, versioned risk configuration.

The version is what makes a proposal's risk decision auditable a year later, so
it has to change when any threshold changes and stay the same when nothing does.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stockbrain.config import Settings
from stockbrain.enums import MarketSession
from stockbrain.risk.config import (
    DEFAULT_ALLOWED_INSTRUMENT_TYPES,
    RiskConfig,
    SpreadPolicy,
    risk_config_from_settings,
)
from tests import risk_helpers as h


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "test", "stockbrain_secret_key": "k"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_version_is_stable_for_identical_configuration() -> None:
    assert RiskConfig().version == RiskConfig().version
    assert len(RiskConfig().version) == 32


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_spread_bps", Decimal("25")),
        ("max_notional_per_trade", Decimal("501")),
        ("min_cash_reserve_pct", Decimal("0.11")),
        ("proposal_ttl_minutes", 31),
        ("allow_fractional_quantity", True),
        ("spread_policy", SpreadPolicy.REDUCE),
        ("allowed_sessions", (MarketSession.REGULAR, MarketSession.PRE_MARKET)),
        ("calibration_modulates_size", False),
        ("calibration_min_samples", 11),
        ("min_calibration_size_factor", Decimal("0.6")),
    ],
)
def test_changing_any_threshold_changes_the_version(field: str, value: object) -> None:
    """Otherwise a proposal could claim to have been judged by limits it wasn't."""
    assert RiskConfig(**{field: value}).version != RiskConfig().version  # type: ignore[arg-type]


def test_the_serialised_config_carries_no_binary_floats() -> None:
    payload = RiskConfig().as_dict()
    for key, value in payload.items():
        assert not isinstance(value, float), f"{key} must not be a float"
    assert payload["max_spread_bps"] == "50"
    assert payload["allowed_sessions"] == ["REGULAR"]


def test_an_unknown_spread_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="spread_policy"):
        RiskConfig(spread_policy="ignore")


def test_the_defaults_are_conservative() -> None:
    """These are limits, not recommendations -- but they should start tight."""
    config = RiskConfig()
    assert config.allowed_instrument_types == DEFAULT_ALLOWED_INSTRUMENT_TYPES
    assert config.allowed_sessions == (MarketSession.REGULAR,)
    assert config.spread_policy == SpreadPolicy.BLOCK
    assert config.require_same_currency is True
    assert config.allow_short_selling is False
    assert config.allow_fractional_quantity is False


def test_settings_build_the_same_defaults() -> None:
    assert risk_config_from_settings(_settings()).version == RiskConfig().version


def test_the_quote_age_limit_is_shared_with_the_market_data_subsystem() -> None:
    """Two numbers that must agree are one number that will eventually disagree."""
    config = risk_config_from_settings(_settings(market_data_max_quote_age_seconds=7.0))
    assert config.max_quote_age_seconds == Decimal("7.0")


def test_environment_overrides_reach_the_config() -> None:
    config = risk_config_from_settings(
        _settings(
            risk_max_spread_bps=Decimal("20"),
            risk_allowed_instrument_types="STOCK",
            risk_allowed_sessions="REGULAR,PRE_MARKET",
            risk_default_quantity_precision=3,
        )
    )
    assert config.max_spread_bps == Decimal("20")
    assert config.allowed_instrument_types == ("STOCK",)
    assert config.allowed_sessions == (MarketSession.REGULAR, MarketSession.PRE_MARKET)
    assert config.default_quantity_precision == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_max_trade_pct", Decimal("1.5")),
        ("risk_min_cash_reserve_pct", Decimal("-0.1")),
        ("risk_min_research_confidence", Decimal("2")),
        ("risk_max_spread_bps", Decimal("0")),
        ("risk_proposal_ttl_minutes", 0),
        ("risk_max_active_proposals", 0),
        ("risk_reduce_fraction", Decimal("0")),
        ("risk_max_notional_per_trade", Decimal("-1")),
        ("risk_default_quantity_precision", 9),
    ],
)
def test_a_meaningless_limit_refuses_to_start(field: str, value: object) -> None:
    """Silently clamping a limit is how a limit stops being the one that was chosen."""
    with pytest.raises(ValidationError, match="risk configuration"):
        _settings(**{field: value})


def test_the_shipped_env_example_loads_and_matches_the_code_defaults() -> None:
    """An operator copies `.env.example`; it must not drift from the code.

    Two failures this catches: a stray inline comment that makes a value
    unparseable, and a documented limit that quietly differs from the default
    the engine would otherwise use.
    """
    root = Path(__file__).resolve().parents[3]
    example = root / ".env.example"
    assert example.is_file()

    settings = Settings(_env_file=str(example))
    assert risk_config_from_settings(settings).as_dict() == RiskConfig().as_dict()
    assert settings.execution_policy.value == "MANUAL"
    assert settings.t212_automated_trading_consent_confirmed is False


def test_exit_thresholds_change_the_policy_version() -> None:
    baseline = h.config()
    stricter = h.config(exit_hard_stop_pct=Decimal("0.05"))
    assert baseline.version != stricter.version


def test_the_roi_decay_table_covers_every_horizon() -> None:
    from stockbrain.enums import TimeHorizon

    covered = {horizon for horizon, _minutes, _target in h.config().exit_roi_decay}
    assert covered == set(TimeHorizon)


def test_the_roi_decay_table_is_non_increasing_within_each_horizon() -> None:
    from itertools import groupby

    rows = sorted(h.config().exit_roi_decay, key=lambda row: (row[0].value, row[1]))
    for _horizon, group in groupby(rows, key=lambda row: row[0]):
        targets = [target for _h, _m, target in group]
        assert targets == sorted(targets, reverse=True)


def test_atr_thresholds_change_the_policy_version() -> None:
    assert h.config().version != h.config(exit_atr_multiplier=Decimal("2.5")).version
    assert h.config().version != h.config(exit_atr_period=20).version
