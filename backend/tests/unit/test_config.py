"""Configuration safety tests.

The live-execution gate is the single most important piece of configuration in
the system, so it is tested exhaustively rather than by example.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from stockbrain.config import BrokerEnvironment, ExecutionMode, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "test"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_defaults_are_demo_and_execution_is_not_permitted() -> None:
    settings = _settings()
    assert settings.t212_env is BrokerEnvironment.DEMO
    assert settings.t212_live_execution_enabled is False
    assert settings.t212_written_consent_confirmed is False
    assert settings.live_execution_permitted is False
    assert settings.t212_base_url == "https://demo.trading212.com/api/v0"


def test_ordinary_settings_do_not_inherit_local_provider_credentials() -> None:
    """The test fixture, not a developer's `.env`, defines the test premise."""
    settings = _settings()
    assert settings.stockbrain_secret_key.get_secret_value() == ""
    assert settings.deepseek_api_key.get_secret_value() == ""
    assert settings.alpaca_api_key.get_secret_value() == ""
    assert settings.alpaca_api_secret.get_secret_value() == ""
    assert settings.brave_api_key.get_secret_value() == ""
    assert settings.exa_api_key.get_secret_value() == ""
    assert settings.firecrawl_api_key.get_secret_value() == ""
    assert settings.sec_contact_email == ""
    assert settings.fred_api_key.get_secret_value() == ""
    assert settings.t212_api_key.get_secret_value() == ""
    assert settings.t212_api_secret.get_secret_value() == ""
    assert settings.telegram_bot_token.get_secret_value() == ""
    assert settings.web_owner_password_hash.get_secret_value() == ""


def test_permitted_and_blockers_can_never_disagree() -> None:
    """The banner and the predicate must be the same fact, not two facts.

    Regression test: all four gates set with no credentials previously reported
    ``live_execution_permitted=True`` while simultaneously listing "credentials
    are not configured" as a blocker.
    """
    for overrides in (
        {},
        {"t212_env": "live"},
        {"t212_env": "live", "t212_api_key": "k", "t212_api_secret": "s"},
        {
            "t212_env": "live",
            "t212_live_execution_enabled": True,
            "t212_written_consent_confirmed": True,
            "execution_mode": "manual_approval",
        },
        {
            "t212_env": "live",
            "t212_live_execution_enabled": True,
            "t212_written_consent_confirmed": True,
            "execution_mode": "manual_approval",
            "t212_api_key": "k",
            "t212_api_secret": "s",
        },
    ):
        settings = _settings(**overrides)
        assert settings.live_execution_permitted == (settings.execution_blockers == []), (
            overrides,
            settings.execution_blockers,
        )


def test_every_gate_set_but_no_credentials_is_not_permitted() -> None:
    """Gates alone do not make execution possible; there must be a key to use."""
    settings = _settings(
        t212_env="live",
        t212_live_execution_enabled=True,
        t212_written_consent_confirmed=True,
        execution_mode="manual_approval",
    )
    assert settings.live_execution_permitted is False
    assert "Trading 212 API credentials are not configured" in settings.execution_blockers


def test_live_execution_requires_every_gate() -> None:
    settings = _settings(
        t212_env="live",
        t212_live_execution_enabled=True,
        t212_written_consent_confirmed=True,
        execution_mode="manual_approval",
        t212_api_key="key",
        t212_api_secret="secret",
    )
    assert settings.live_execution_permitted is True
    assert settings.t212_base_url == "https://live.trading212.com/api/v0"
    assert settings.execution_blockers == []


@pytest.mark.parametrize(
    ("env", "consent", "mode"),
    [
        combo
        for combo in itertools.product(
            ["demo", "live"], [True, False], ["manual_approval", "research_only"]
        )
        # Exclude the single fully-permitted combination.
        if combo != ("live", True, "manual_approval")
    ],
)
def test_no_other_combination_permits_live_execution(env: str, consent: bool, mode: str) -> None:
    """Enabling live execution alongside any contradictory flag must not start.

    The safe resolution of an ambiguous execution configuration is to refuse,
    never to fall back to live.
    """
    with pytest.raises(ValidationError):
        _settings(
            t212_env=env,
            t212_live_execution_enabled=True,
            t212_written_consent_confirmed=consent,
            execution_mode=mode,
        )


def test_live_environment_without_enable_flag_is_still_blocked() -> None:
    """T212_ENV=live alone must not permit execution; it is a read-only posture."""
    settings = _settings(
        t212_env="live",
        t212_written_consent_confirmed=True,
        t212_api_key="key",
        t212_api_secret="secret",
    )
    assert settings.live_execution_permitted is False
    assert "T212_LIVE_EXECUTION_ENABLED is false" in settings.execution_blockers


def test_research_only_mode_blocks_execution() -> None:
    settings = _settings(execution_mode="research_only")
    assert settings.execution_mode is ExecutionMode.RESEARCH_ONLY
    assert settings.live_execution_permitted is False


def test_blockers_explain_missing_credentials() -> None:
    settings = _settings()
    assert "Trading 212 API credentials are not configured" in settings.execution_blockers


def test_database_url_must_use_asyncpg() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="postgresql://user:pass@localhost:5432/stockbrain")


def test_telegram_allowlists_accept_comma_separated_ids() -> None:
    settings = _settings(
        telegram_allowed_user_ids="123, 456 ,789",
        telegram_allowed_chat_ids="-100123",
    )
    assert settings.telegram_allowed_user_ids == [123, 456, 789]
    assert settings.telegram_allowed_chat_ids == [-100123]


def test_telegram_allowlist_defaults_to_empty() -> None:
    """An unset allowlist authorises nobody, rather than everybody."""
    settings = _settings()
    assert settings.telegram_allowed_user_ids == []
    assert settings.telegram_allowed_chat_ids == []


def test_production_requires_secret_key() -> None:
    with pytest.raises(ValidationError, match="STOCKBRAIN_SECRET_KEY"):
        Settings(app_env="production", stockbrain_secret_key="")


def test_secrets_do_not_appear_in_repr() -> None:
    settings = _settings(t212_api_secret="hunter2-super-secret", deepseek_api_key="sk-abcdef")
    rendered = repr(settings) + str(settings)
    assert "hunter2-super-secret" not in rendered
    assert "sk-abcdef" not in rendered


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        _settings(log_level="chatty")


def test_list_fields_parse_from_plain_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.env files carry ``a,b`` rather than JSON; both forms must work.

    Regression test: pydantic-settings JSON-decodes complex types from the
    environment before field validators run, which made a plain
    comma-separated value a startup crash.
    """
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111,222")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100999")
    settings = Settings(app_env="test")

    assert settings.cors_allow_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert settings.telegram_allowed_user_ids == [111, 222]
    assert settings.telegram_allowed_chat_ids == [-100999]


def test_list_fields_still_accept_json_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", '["333", "444"]')
    assert Settings(app_env="test").telegram_allowed_user_ids == [333, 444]


def test_empty_list_environment_value_yields_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    assert Settings(app_env="test").telegram_allowed_user_ids == []


def test_env_file_values_ignore_inline_comments_and_quotes(tmp_path: Path) -> None:
    """A commented `.env` line must not become part of a credential.

    The shipped `.env.example` annotates values inline (``ALPACA_STOCK_FEED=iex
    # iex | sip | delayed_sip``), so a parser that kept the comment would turn a
    valid feed into an invalid one -- and, worse, would silently append a
    comment to an API key, which surfaces later as an unexplained auth failure
    rather than as a configuration error.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a full-line comment",
                "ALPACA_STOCK_FEED=iex               # iex | sip | delayed_sip",
                'ALPACA_API_KEY="quoted-key"',
                "T212_API_SECRET=plain-secret  # trailing note",
                "MARKET_DATA_PROBE_SYMBOL=MSFT",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env_file))

    assert settings.alpaca_stock_feed == "iex"
    assert settings.alpaca_api_key.get_secret_value() == "quoted-key"
    assert settings.t212_api_secret.get_secret_value() == "plain-secret"
    assert settings.market_data_probe_symbol == "MSFT"


def test_a_trailing_floor_wider_than_its_arm_threshold_is_refused(
    make_settings: Any,
) -> None:
    with pytest.raises(ValidationError, match="already be under the entry price"):
        make_settings(
            RISK_EXIT_TRAILING_PCT="0.20",
            RISK_EXIT_TRAILING_ARM_PCT="0.10",
        )
