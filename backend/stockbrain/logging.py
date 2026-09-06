"""Structured logging with mandatory secret redaction.

Two independent defences are applied to every log event:

1. **Key-based redaction** -- any key whose name looks like a credential
   (``authorization``, ``api_key``, ``token``, ``password``, ...) has its value
   replaced, recursively, inside nested dicts and lists.
2. **Value-based redaction** -- the literal secret values loaded from
   configuration are scrubbed out of *any* rendered string.  This catches the
   case where a credential is interpolated into a URL, an exception message, or
   a provider error body.

Neither defence excuses logging a secret deliberately; they exist so that an
accidental ``log.info("request", headers=headers)`` cannot leak one.
"""

from __future__ import annotations

import logging
import sys
import warnings
from collections.abc import Iterable, MutableMapping
from typing import Any

import structlog
from pydantic import SecretStr
from structlog.types import EventDict, Processor

from stockbrain.config import LogFormat, Settings, get_settings
from stockbrain.observability.logstore import LogBuffer, LogCaptureProcessor

__all__ = [
    "bind_correlation",
    "configure_logging",
    "get_logger",
    "log_buffer",
]

REDACTED = "***REDACTED***"

_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
)

_MIN_REDACTABLE_SECRET_LENGTH = 6
"""Below this length a "secret" is more likely to be a placeholder than a credential,
and scrubbing it would corrupt unrelated log text."""


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _redact_by_key(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret_key(key) else _redact_by_key(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_by_key(item) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    return value


class SecretScrubber:
    """structlog processor that removes known credential values from rendered text."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted(
            {s for s in secrets if len(s) >= _MIN_REDACTABLE_SECRET_LENGTH},
            key=len,
            reverse=True,
        )

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            rebuilt = [self._scrub(item) for item in value]
            return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
        return value

    def __call__(
        self, _logger: object, _name: str, event_dict: MutableMapping[str, Any]
    ) -> EventDict:
        redacted = _redact_by_key(dict(event_dict))
        if self._secrets:
            redacted = self._scrub(redacted)
        return dict(redacted)


def _collect_secret_values(settings: Settings) -> list[str]:
    values: list[str] = []
    for name in type(settings).model_fields:
        candidate = getattr(settings, name, None)
        if isinstance(candidate, SecretStr):
            revealed = candidate.get_secret_value()
            if revealed:
                values.append(revealed)
    return values


#: The process-wide log buffer the GUI reads.  Replaced wholesale by
#: :func:`configure_logging` so its capacity follows configuration, and exposed
#: through :func:`log_buffer` rather than imported directly, because a module
#: that captured the object at import time would keep querying the buffer the
#: *previous* configuration built.
_LOG_BUFFER = LogBuffer(0)


def log_buffer() -> LogBuffer:
    """The buffer the current logging configuration is writing into."""
    return _LOG_BUFFER


def configure_logging(settings: Settings | None = None) -> None:
    """Install the structlog + stdlib logging configuration.  Idempotent."""
    global _LOG_BUFFER
    settings = settings or get_settings()

    scrubber = SecretScrubber(_collect_secret_values(settings))
    _LOG_BUFFER = LogBuffer(settings.log_buffer_size, min_level=settings.log_level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        # The traceback is rendered into `exception` *before* the scrubber runs,
        # which is the whole point of this ordering. Previously the scrubber ran
        # first and the exception was formatted afterwards -- by the renderer in
        # console mode, and by `format_exc_info` in JSON mode -- so a credential
        # inside an exception *message* ("auth failed for sk-live-...") reached
        # stdout, and therefore `docker logs` and any shipper, unredacted. Both
        # formats now render it here so both are scrubbed.
        structlog.dev.set_exc_info,
        structlog.processors.format_exc_info,
        scrubber,
    ]

    # ConsoleRenderer warns that it cannot pretty-print exceptions while
    # `format_exc_info` is in the chain. That is the deliberate trade made
    # above -- a plain traceback that is scrubbed beats a pretty one that
    # leaks -- and it is not something an operator can act on.
    warnings.filterwarnings(
        "ignore",
        message="Remove `format_exc_info` from your processor chain",
        category=UserWarning,
        module="structlog.*",
    )

    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if settings.log_format is LogFormat.CONSOLE
        else structlog.processors.JSONRenderer()
    )

    # Last in the chain on purpose: by this point the event carries its level,
    # its logger name, an ISO timestamp and any rendered exception. It scrubs
    # the dictionary again before keeping it -- redundant now that the scrubber
    # runs after `format_exc_info`, and kept because the buffer is served over
    # HTTP and a reordering of this list must not silently turn that into a
    # leak.
    shared.append(LogCaptureProcessor(_LOG_BUFFER, scrubber))

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # uvicorn ships its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_correlation(**identifiers: object) -> None:
    """Bind correlation identifiers (``event_id``, ``proposal_id``, ``job_id``, ...).

    Values are bound to the current asyncio context, so every subsequent log line
    in the same task carries them.
    """
    structlog.contextvars.bind_contextvars(
        **{key: str(value) for key, value in identifiers.items() if value is not None}
    )
