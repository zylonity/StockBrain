"""Tests for the standalone configuration validator.

The container entrypoint runs this before the database wait loop, so a
misconfiguration fails fast instead of being retried for two minutes as if it
were a connectivity problem.
"""

from __future__ import annotations

import pytest

from stockbrain.validate_config import main


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "T212_ENV",
        "T212_LIVE_EXECUTION_ENABLED",
        "T212_WRITTEN_CONSENT_CONFIRMED",
        "EXECUTION_MODE",
        "T212_API_KEY",
        "T212_API_SECRET",
        "APP_ENV",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valid_configuration_returns_zero_and_reports_posture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_env(monkeypatch)
    assert main() == 0
    out = capsys.readouterr().out
    assert "configuration OK" in out
    assert "live execution permitted: False" in out
    assert "blocked:" in out


def test_contradictory_live_execution_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("T212_LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("T212_ENV", "demo")

    assert main() == 1
    err = capsys.readouterr().err
    assert "CONFIGURATION ERROR" in err
    assert "refuses to start" in err
    assert "T212_ENV must be 'live'" in err


def test_error_output_contains_no_stack_trace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("T212_LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("T212_ENV", "demo")

    main()
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "pydantic_core" not in err


def test_error_output_never_echoes_secret_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pydantic's error `input` holds the whole settings dict; it must not be printed."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("T212_LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("T212_ENV", "demo")
    monkeypatch.setenv("T212_API_SECRET", "super-secret-value-do-not-log")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-appear-anywhere")

    main()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "super-secret-value-do-not-log" not in combined
    assert "sk-must-not-appear-anywhere" not in combined


def test_invalid_database_url_is_reported_clearly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/stockbrain")

    assert main() == 1
    err = capsys.readouterr().err
    assert "database_url" in err
    assert "asyncpg" in err
    # The URL contains a password; the validator must not echo the input.
    assert "pass@localhost" not in err
