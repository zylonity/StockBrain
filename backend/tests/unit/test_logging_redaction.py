"""Secret redaction tests.

Two layers are tested independently and then together through the real logging
stack:

* key-based redaction, which catches ``{"Authorization": ...}`` regardless of
  the value;
* value-based scrubbing, which catches a configured credential interpolated
  into a URL or an exception message, where no key name gives it away.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog

from stockbrain.config import Settings
from stockbrain.logging import REDACTED, SecretScrubber, configure_logging

# ---------------------------------------------------------------------------
# Processor-level tests
# ---------------------------------------------------------------------------


def _scrub(event: dict[str, Any], secrets: list[str] | None = None) -> MutableMapping[str, Any]:
    return SecretScrubber(secrets or [])(None, "info", event)


def test_authorization_key_is_redacted_whatever_the_case() -> None:
    result = _scrub({"headers": {"Authorization": "Basic dXNlcjpwYXNz"}})
    assert result["headers"]["Authorization"] == REDACTED

    result = _scrub({"headers": {"authorization": "Basic dXNlcjpwYXNz"}})
    assert result["headers"]["authorization"] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apiKey",
        "api_secret",
        "T212_API_SECRET",
        "password",
        "bot_token",
        "x-api-key",
        "set-cookie",
        "db_credential",
    ],
)
def test_credential_shaped_keys_are_redacted(key: str) -> None:
    assert _scrub({key: "value-that-must-not-appear"})[key] == REDACTED


def test_redaction_recurses_through_dicts_and_lists() -> None:
    result = _scrub(
        {
            "requests": [
                {"url": "https://example.test", "headers": {"authorization": "secret"}},
                {"nested": {"deep": {"token": "secret"}}},
            ]
        }
    )
    assert result["requests"][0]["headers"]["authorization"] == REDACTED
    assert result["requests"][1]["nested"]["deep"]["token"] == REDACTED
    assert result["requests"][0]["url"] == "https://example.test"


def test_configured_secret_values_are_scrubbed_from_free_text() -> None:
    result = _scrub(
        {"message": "GET https://api.example.test?key=sk-abcdef123456 failed"},
        ["sk-abcdef123456"],
    )
    assert "sk-abcdef123456" not in result["message"]
    assert REDACTED in result["message"]


def test_short_values_are_not_scrubbed() -> None:
    """A two-character 'secret' would corrupt unrelated text if scrubbed."""
    result = _scrub({"message": "the demo account"}, ["de"])
    assert result["message"] == "the demo account"


def test_non_credential_values_survive() -> None:
    result = _scrub({"proposal_id": "abc", "quantity": 0.42, "side": "BUY"})
    assert result == {"proposal_id": "abc", "quantity": 0.42, "side": "BUY"}


# ---------------------------------------------------------------------------
# End-to-end through the configured logging stack
# ---------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    settings = Settings(
        app_env="test",
        log_level="DEBUG",
        log_format="json",
        t212_api_secret="t212-secret-value-abcdef",
        deepseek_api_key="sk-deepseek-abcdef123456",
        telegram_bot_token="1234567:AA-telegram-token-value",
    )
    configure_logging(settings)
    buffer = io.StringIO()
    root = logging.getLogger()
    # Redirect the handler installed by configure_logging so the rendered output
    # is inspectable without depending on pytest's capture internals.
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)
    try:
        yield buffer
    finally:
        root.handlers.clear()
        structlog.reset_defaults()


def _records(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().strip().splitlines() if line]


def test_end_to_end_header_redaction(log_stream: io.StringIO) -> None:
    structlog.get_logger("test").info(
        "outbound",
        headers={"Authorization": "Basic dXNlcjpwYXNz", "Accept": "application/json"},
    )
    record = _records(log_stream)[-1]
    assert record["headers"]["Authorization"] == REDACTED
    assert record["headers"]["Accept"] == "application/json"
    assert record["event"] == "outbound"


def test_end_to_end_value_scrubbing_of_every_configured_secret(
    log_stream: io.StringIO,
) -> None:
    structlog.get_logger("test").warning(
        "provider_error",
        deepseek="https://api.deepseek.com?key=sk-deepseek-abcdef123456",
        telegram="https://api.telegram.org/bot1234567:AA-telegram-token-value/getUpdates",
        broker="basic auth for t212-secret-value-abcdef",
    )
    rendered = json.dumps(_records(log_stream)[-1])
    for secret in (
        "sk-deepseek-abcdef123456",
        "AA-telegram-token-value",
        "t212-secret-value-abcdef",
    ):
        assert secret not in rendered


def test_end_to_end_output_is_valid_json_with_utc_timestamp(
    log_stream: io.StringIO,
) -> None:
    structlog.get_logger("test").info("event", proposal_id="abc")
    record = _records(log_stream)[-1]
    assert record["level"] == "info"
    assert record["timestamp"].endswith("Z")
    assert record["proposal_id"] == "abc"
