"""The bounded log buffer, its filters, and the secrets it must never keep.

The buffer is a feature with a security surface: it takes everything the
application logs and makes it readable over HTTP.  So the tests that matter are
the ones about what it *refuses* -- unbounded growth, unscrubbed exception text,
and a level below the configured floor.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from stockbrain.config import Settings
from stockbrain.logging import REDACTED, configure_logging, get_logger, log_buffer
from stockbrain.observability.logstore import (
    LogBuffer,
    LogCaptureProcessor,
    LogQuery,
    service_for_logger,
)


def _entry(
    buffer: LogBuffer,
    event: str,
    *,
    level: str = "info",
    logger: str = "stockbrain.ingestion.brave",
    **fields: object,
) -> None:
    processor = LogCaptureProcessor(buffer)
    payload: dict[str, object] = {
        "event": event,
        "level": level,
        "logger": logger,
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        **fields,
    }
    processor(None, level, payload)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_the_buffer_never_grows_past_its_capacity() -> None:
    buffer = LogBuffer(10)
    for index in range(100):
        _entry(buffer, f"event-{index}")

    result = buffer.query(LogQuery(limit=500))
    assert result.stored == 10
    assert result.total == 10
    assert result.dropped == 90
    # Newest first, and the oldest ninety are genuinely gone rather than hidden.
    assert result.entries[0].event == "event-99"


def test_a_zero_capacity_buffer_captures_nothing() -> None:
    buffer = LogBuffer(0)
    _entry(buffer, "should-not-be-kept")
    assert buffer.enabled is False
    assert buffer.query(LogQuery()).stored == 0


def test_entries_below_the_configured_level_are_not_captured() -> None:
    """The floor is the process's own log level, not a display filter.

    A buffer that captured DEBUG while stdout did not would show the operator
    lines that exist nowhere else, and would cost the memory of a level the
    deployment deliberately switched off.
    """
    buffer = LogBuffer(50, min_level="warning")
    _entry(buffer, "routine", level="info")
    _entry(buffer, "problem", level="error")

    events = [entry.event for entry in buffer.query(LogQuery()).entries]
    assert events == ["problem"]


def test_one_pathological_field_cannot_consume_the_buffer() -> None:
    buffer = LogBuffer(5)
    _entry(buffer, "huge", body="x" * 10_000)
    entry = buffer.query(LogQuery()).entries[0]
    assert len(entry.fields["body"]) <= 400


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_filters_narrow_and_never_widen() -> None:
    buffer = LogBuffer(100)
    _entry(buffer, "brave_search", logger="stockbrain.ingestion.brave")
    _entry(buffer, "exa_search", logger="stockbrain.ingestion.exa")
    _entry(buffer, "order_sent", logger="stockbrain.execution.service", level="warning")

    assert {e.event for e in buffer.query(LogQuery(services=("brave",))).entries} == {
        "brave_search"
    }
    assert {e.event for e in buffer.query(LogQuery(categories=("execution",))).entries} == {
        "order_sent"
    }
    assert {e.event for e in buffer.query(LogQuery(min_level="warning")).entries} == {"order_sent"}
    assert {e.event for e in buffer.query(LogQuery(search="EXA")).entries} == {"exa_search"}


def test_search_matches_field_values_not_only_the_event_name() -> None:
    buffer = LogBuffer(100)
    _entry(buffer, "job_failed", job_type="RUN_RESEARCH")
    _entry(buffer, "job_failed", job_type="CLASSIFY_EVENT")
    found = buffer.query(LogQuery(search="run_research")).entries
    assert len(found) == 1
    assert found[0].fields["job_type"] == "RUN_RESEARCH"


def test_a_time_window_excludes_older_entries() -> None:
    buffer = LogBuffer(100)
    _entry(buffer, "old")
    cutoff = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
    assert buffer.query(LogQuery(since=cutoff)).total == 0


def test_paging_is_stable_and_reports_the_full_total() -> None:
    buffer = LogBuffer(100)
    for index in range(10):
        _entry(buffer, f"event-{index}")
    first = buffer.query(LogQuery(limit=4, offset=0))
    second = buffer.query(LogQuery(limit=4, offset=4))
    assert first.total == second.total == 10
    assert {e.sequence for e in first.entries}.isdisjoint({e.sequence for e in second.entries})


def test_facets_report_only_what_is_present() -> None:
    buffer = LogBuffer(100)
    _entry(buffer, "a", logger="stockbrain.ingestion.brave")
    _entry(buffer, "b", logger="stockbrain.ingestion.brave")
    _entry(buffer, "c", logger="stockbrain.fx.service")
    facets = buffer.facets()
    assert facets.services == {"brave": 2, "fx": 1}
    assert facets.categories == {"discovery": 2, "fx": 1}


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logger", "service", "category"),
    [
        ("stockbrain.ingestion.brave", "brave", "discovery"),
        ("stockbrain.ingestion.service", "ingestion", "discovery"),
        ("stockbrain.execution.service", "execution", "execution"),
        ("stockbrain.telegram.notifier", "telegram", "notifications"),
        ("stockbrain.observability.alerts", "alerts", "notifications"),
        ("stockbrain.llm.openai_compat", "llm", "intelligence"),
        ("something.entirely.unknown", "app", "system"),
    ],
)
def test_logger_names_resolve_to_the_health_board_vocabulary(
    logger: str, service: str, category: str
) -> None:
    """Longest prefix wins, so a provider module beats its package."""
    assert service_for_logger(logger) == (service, category)


def test_an_explicit_provider_field_beats_the_module() -> None:
    """A job runner logging ``provider="brave"`` is a Brave event."""
    buffer = LogBuffer(10)
    _entry(buffer, "provider_call", logger="stockbrain.jobs.handlers", provider="brave")
    assert buffer.query(LogQuery()).entries[0].service == "brave"


def test_an_arbitrary_provider_field_cannot_invent_a_service() -> None:
    """Otherwise a log line could create a facet nothing else in the UI knows."""
    buffer = LogBuffer(10)
    _entry(buffer, "call", logger="stockbrain.jobs.handlers", provider="not-a-service")
    assert buffer.query(LogQuery()).entries[0].service == "jobs"


# ---------------------------------------------------------------------------
# Redaction -- through the real logging stack
# ---------------------------------------------------------------------------


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    stream = io.StringIO()
    settings = Settings(
        app_env="test",
        stockbrain_secret_key="unit-test-signing-key",
        telegram_bot_token="1234:super-secret-bot-token",
        log_level="INFO",
        log_format="json",
        log_buffer_size=100,
    )
    configure_logging(settings)
    # Located by its formatter rather than by index: pytest's logging plugin
    # installs handlers of its own, and `handlers[0]` is whichever of them ran
    # first in the whole session.
    handler = next(
        candidate
        for candidate in logging.getLogger().handlers
        if isinstance(candidate.formatter, structlog.stdlib.ProcessorFormatter)
    )
    monkeypatch.setattr(handler, "stream", stream, raising=False)
    yield stream
    structlog.reset_defaults()


def test_a_credential_shaped_key_never_reaches_the_buffer(configured: io.StringIO) -> None:
    get_logger("stockbrain.ingestion.brave").info("call", headers={"Authorization": "Bearer abc"})
    entry = log_buffer().query(LogQuery()).entries[0]
    assert "Bearer abc" not in json.dumps(entry.fields)
    assert REDACTED in json.dumps(entry.fields)


def test_a_configured_secret_value_never_reaches_the_buffer(configured: io.StringIO) -> None:
    get_logger("stockbrain.telegram.notifier").warning(
        "send_failed", url="https://api.telegram.org/bot1234:super-secret-bot-token/sendMessage"
    )
    entry = log_buffer().query(LogQuery()).entries[0]
    assert "super-secret-bot-token" not in json.dumps(entry.fields)


def test_exception_text_is_scrubbed_before_it_is_stored(configured: io.StringIO) -> None:
    """The capture runs after ``format_exc_info``, so it must scrub again.

    The rendered traceback is added to the event *after* the shared scrubber has
    run.  Without a second pass the buffer would be the one place in the system
    where a credential in an exception message survives.
    """
    try:
        raise RuntimeError("token 1234:super-secret-bot-token rejected")
    except RuntimeError:
        get_logger("stockbrain.llm.openai_compat").exception("llm_call_failed")

    entry = log_buffer().query(LogQuery()).entries[0]
    assert "super-secret-bot-token" not in entry.message
    assert REDACTED in entry.message


def test_capture_does_not_change_what_reaches_stdout(configured: io.StringIO) -> None:
    """The buffer is a copy, never a filter.

    stdout is the fallback an operator uses when the Logs page is the thing that
    is broken, so a processor that could drop or rewrite an event would be
    removing the safety net.
    """
    get_logger("stockbrain.main").info("startup_complete", database_ok=True)
    rendered = json.loads(configured.getvalue().strip().splitlines()[-1])
    assert rendered["event"] == "startup_complete"
    assert rendered["database_ok"] is True
    assert log_buffer().query(LogQuery()).entries[0].event == "startup_complete"
