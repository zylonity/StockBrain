"""Lifecycle: the bot must never be able to hold the application back.

Three things are asserted here and nowhere else:

* a deployment without a token, or without an allowlist, builds **no** runtime
  at all and reports ``DISABLED`` -- there is no half-configured bot;
* a Telegram outage degrades Telegram and nothing else: the supervisor backs
  off in its own task while everything else keeps running;
* a rejected token is fatal rather than retried forever, mirroring how a
  provider auth failure is treated everywhere else in this system.
"""

from __future__ import annotations

import asyncio

import pytest
from telegram.error import InvalidToken, NetworkError

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.session import Database
from stockbrain.enums import ProviderStatus
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.telegram import runtime as runtime_module
from stockbrain.telegram.handlers import ALLOWED_UPDATES
from stockbrain.telegram.runtime import TelegramRuntime, error_category

TOKEN = "8000000000:AA-not-a-real-bot-token-value-xyz"
OWNER = 4242


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "stockbrain_secret_key": "test-secret-key",
        "telegram_enabled": True,
        "telegram_bot_token": TOKEN,
        "telegram_allowed_user_ids": str(OWNER),
        "telegram_health_interval_seconds": 10.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _blockers(status: dict[str, object]) -> list[str]:
    blockers = status["blockers"]
    assert isinstance(blockers, list)
    return [str(blocker) for blocker in blockers]


def _runtime(settings: Settings) -> tuple[TelegramRuntime, ProviderHealthRegistry]:
    health = ProviderHealthRegistry()
    database = Database(settings)
    return (
        TelegramRuntime(
            settings,
            database,
            health=health,
            proposals=None,
            control=ControlStateService(database),
        ),
        health,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_a_missing_token_disables_the_bot_without_starting_anything() -> None:
    settings = _settings(telegram_bot_token="")
    runtime, _ = _runtime(settings)
    assert not runtime.enabled
    status = runtime.status()
    assert status["status"] == ProviderStatus.DISABLED.value
    assert status["bot_configured"] is False
    assert "TELEGRAM_BOT_TOKEN is not set" in _blockers(status)


def test_an_empty_allowlist_disables_the_bot() -> None:
    runtime, _ = _runtime(_settings(telegram_allowed_user_ids=""))
    assert not runtime.enabled
    assert any("authorises nobody" in blocker for blocker in _blockers(runtime.status()))


async def test_starting_a_disabled_runtime_records_disabled_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application must come up normally with no bot configured."""
    settings = _settings(telegram_enabled=False)
    runtime, health = _runtime(settings)

    launched = False

    async def _never() -> None:  # pragma: no cover - asserted not to run
        nonlocal launched
        launched = True

    monkeypatch.setattr(runtime, "_launch", _never)
    await runtime.start()
    await runtime.stop()

    assert launched is False
    assert health.get(ProviderName.TELEGRAM).status is ProviderStatus.DISABLED


def test_the_status_payload_contains_no_token_and_no_secret() -> None:
    """Not even the bot's numeric id.

    A Telegram bot id is the part of the token *before* the colon, so reporting
    it would publish half the credential in a health response and in the startup
    log line. The runtime therefore keeps a boolean and discards the identity
    ``getMe`` returns -- asserted here against a runtime that has been told it
    launched, because the earlier version of this test only passed by virtue of
    never having started.
    """
    runtime, _ = _runtime(_settings())
    runtime._bot_identified = True
    rendered = repr(runtime.status())
    assert TOKEN not in rendered
    assert TOKEN.split(":", 1)[0] not in rendered
    # Only whether one is configured, whether it authenticated, and how many
    # identities are allowed.
    assert runtime.status()["bot_configured"] is True
    assert runtime.status()["bot_identified"] is True
    assert runtime.status()["authorized_users"] == 1
    assert "bot_id" not in runtime.status()
    assert "bot_username" not in runtime.status()


def test_the_transport_is_long_polling_and_no_webhook_is_configured() -> None:
    """No inbound port, no public TLS endpoint, nothing to expose."""
    status = _runtime(_settings())[0].status()
    assert status["transport"] == "long_polling"
    assert status["webhook_configured"] is False


def test_only_the_two_needed_update_types_are_requested() -> None:
    assert ALLOWED_UPDATES == ("message", "callback_query")


def test_an_error_category_is_a_class_name_and_never_a_message() -> None:
    """A python-telegram-bot error message can carry the request URL.

    The request URL carries the bot token, so only the class name is recorded.
    """
    exc = NetworkError(f"httpx.ConnectError for https://api.telegram.org/bot{TOKEN}/getUpdates")
    assert error_category(exc) == "NetworkError"
    assert TOKEN not in error_category(exc)


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------
async def test_a_start_failure_degrades_telegram_and_leaves_the_task_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable Telegram must not stop FastAPI or the job workers."""
    monkeypatch.setattr(runtime_module, "_INITIAL_BACKOFF_SECONDS", 0.01)
    runtime, health = _runtime(_settings(telegram_max_backoff_seconds=1.0))

    attempts = 0

    async def _fail() -> None:
        nonlocal attempts
        attempts += 1
        raise NetworkError("connection refused")

    monkeypatch.setattr(runtime, "_launch", _fail)
    await runtime.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if attempts >= 2:
            break
    assert attempts >= 2, "the supervisor should retry rather than give up"

    state = health.get(ProviderName.TELEGRAM)
    # Degraded after the first failure, down once a run of them establishes
    # that it is not coming back on its own. Either is correct here; what
    # matters is that it is not reported healthy and not reported unknown.
    assert state.status in {ProviderStatus.DEGRADED, ProviderStatus.DOWN}
    assert state.detail == "NetworkError"
    await runtime.stop()


async def test_a_rejected_token_is_fatal_rather_than_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same judgement as ``ProviderAuthError``: a bad credential is not transient."""
    monkeypatch.setattr(runtime_module, "_INITIAL_BACKOFF_SECONDS", 0.01)
    runtime, health = _runtime(_settings())

    attempts = 0

    async def _reject() -> None:
        nonlocal attempts
        attempts += 1
        raise InvalidToken("Unauthorized")

    monkeypatch.setattr(runtime, "_launch", _reject)
    await runtime.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.status()["fatal"]:
            break

    assert attempts == 1
    assert runtime.status()["status"] == ProviderStatus.DOWN.value
    assert runtime.status()["polling"] is False
    assert health.get(ProviderName.TELEGRAM).status is ProviderStatus.DOWN
    await runtime.stop()


async def test_stop_is_safe_before_start_and_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _ = _runtime(_settings())
    await runtime.stop()
    await runtime.stop()


async def test_a_poll_error_callback_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """python-telegram-bot documents that a raising error callback aborts its
    retry loop, which would turn a transient blip into a dead bot."""
    runtime, health = _runtime(_settings())
    runtime._on_poll_error(NetworkError("blip"))
    assert health.get(ProviderName.TELEGRAM).status is ProviderStatus.DEGRADED
    assert runtime.status()["consecutive_failures"] == 1
    assert runtime.status()["last_error_category"] == "NetworkError"


# ---------------------------------------------------------------------------
# Container wiring
# ---------------------------------------------------------------------------
def test_the_service_container_builds_no_runtime_when_telegram_is_unavailable() -> None:
    from stockbrain.services import ServiceContainer

    settings = _settings(telegram_enabled=False)
    health = ProviderHealthRegistry()
    container = ServiceContainer(settings=settings, database=Database(settings), health=health)
    assert container.telegram is None
    assert container.control is not None


def test_the_service_container_builds_a_runtime_when_it_is_configured() -> None:
    from stockbrain.services import ServiceContainer

    settings = _settings()
    container = ServiceContainer(
        settings=settings, database=Database(settings), health=ProviderHealthRegistry()
    )
    assert container.telegram is not None
    assert container.telegram.enabled


def test_the_runtime_never_receives_a_broker_credential() -> None:
    """The bot's dependencies are a database, health, control and proposals.

    None of them is a broker client, and the proposal service it holds has no
    order path -- so there is no credential for a chat handler to reach.
    """
    import inspect

    source = inspect.getsource(runtime_module)
    for forbidden in ("t212_api_key", "t212_api_secret", "Trading212", "alpaca_api"):
        assert forbidden not in source

    signature = inspect.signature(TelegramRuntime.__init__)
    assert set(signature.parameters) == {
        "self",
        "settings",
        "database",
        "health",
        "proposals",
        "control",
        # Which notification categories are switched on. Reads and writes one
        # `app_settings` row; it is not a client of anything.
        "preferences",
    }
