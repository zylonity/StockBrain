"""A bounded, queryable buffer of the application's own structured logs.

StockBrain runs unattended on a NAS.  When something goes wrong the operator is
not sitting at a terminal with ``docker compose logs`` open, and the useful
question -- "what did the Brave provider do in the last hour?" -- is not one a
scrollback answers.  This module keeps the most recent structured log events in
process memory so the GUI can filter them by service, severity, category, time
and free text.

Four properties define it, and each one is a deliberate refusal:

**Bounded.**  A ``deque`` with a fixed ``maxlen``.  The oldest entry is dropped
when a new one arrives, so a log storm costs a known, constant amount of memory
rather than an unbounded amount of it.  ``LOG_BUFFER_SIZE=0`` disables capture
entirely.

**Never a filesystem.**  There is no path parameter anywhere in this module and
no reader of any file.  A "view the logs" feature that took a filename would be
an arbitrary-file-read endpoint wearing a hat.

**Redacted before it is stored, not before it is shown.**  The capture processor
runs the same :class:`~stockbrain.logging.SecretScrubber` the renderer uses,
over the entry it is about to keep -- including the rendered exception text,
which the stdout renderer sees *after* scrubbing and this buffer would otherwise
keep unscrubbed.  A secret that never enters the buffer cannot leave it.

**In memory only.**  Entries do not survive a restart, and the API says so with
``captured_since``.  Persisting them would mean a write on the hot path of every
log call and a retention sweep to bound the table; that is a larger change than
the question "what is happening right now" needs, and an honest empty buffer is
better than a silently truncated table.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import threading
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from structlog.types import EventDict


def utcnow() -> dt.datetime:
    """Timezone-aware now.

    Defined locally rather than imported from :mod:`stockbrain.db.base` so this
    module stays importable by :mod:`stockbrain.logging` without pulling
    SQLAlchemy into the logging import graph.
    """
    return dt.datetime.now(dt.UTC)


__all__ = [
    "LEVEL_SEVERITY",
    "LOG_CATEGORIES",
    "LogBuffer",
    "LogCaptureProcessor",
    "LogEntry",
    "LogFacets",
    "LogQuery",
    "LogQueryResult",
    "service_for_logger",
]

#: Severity ordering used by the ``min_level`` filter.  The stdlib numbers are
#: reused rather than invented so a level this table does not name still sorts
#: correctly.
LEVEL_SEVERITY: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "exception": logging.ERROR,
    "notset": logging.NOTSET,
}

#: Coarse functional groupings.  Deliberately the operator's vocabulary rather
#: than the package layout: an operator asks "what is discovery doing", not
#: "what is ``stockbrain.ingestion`` doing".
LOG_CATEGORIES: tuple[str, ...] = (
    "system",
    "api",
    "database",
    "jobs",
    "discovery",
    "extraction",
    "intelligence",
    "research",
    "instruments",
    "market_data",
    "fx",
    "risk",
    "proposals",
    "execution",
    "broker",
    "notifications",
    "observability",
)

#: Module prefix -> (service, category).  Longest prefix wins, so
#: ``stockbrain.ingestion.brave`` resolves to the ``brave`` provider while
#: ``stockbrain.ingestion.service`` resolves to the generic ``ingestion``
#: service.  The service half is what the System Health page links to, which is
#: why provider modules map onto the same names
#: :class:`~stockbrain.observability.health.ProviderName` uses.
_MODULE_SERVICES: tuple[tuple[str, str, str], ...] = (
    ("stockbrain.ingestion.alpaca_news", "alpaca_news", "discovery"),
    ("stockbrain.ingestion.brave", "brave", "discovery"),
    ("stockbrain.ingestion.exa", "exa", "discovery"),
    ("stockbrain.ingestion.firecrawl", "firecrawl", "discovery"),
    ("stockbrain.ingestion.sec_edgar", "sec", "discovery"),
    ("stockbrain.ingestion.provider_budget", "provider_budget", "discovery"),
    ("stockbrain.ingestion.web_search", "web_discovery", "discovery"),
    ("stockbrain.ingestion", "ingestion", "discovery"),
    ("stockbrain.extraction.firecrawl", "firecrawl", "extraction"),
    ("stockbrain.extraction", "content_extraction", "extraction"),
    ("stockbrain.intelligence.research", "research", "research"),
    ("stockbrain.intelligence.tradingagents", "tradingagents", "research"),
    ("stockbrain.intelligence.classifier", "classifier", "intelligence"),
    ("stockbrain.intelligence.semantic_dedupe", "semantic_dedupe", "intelligence"),
    ("stockbrain.intelligence", "intelligence", "intelligence"),
    ("stockbrain.llm", "llm", "intelligence"),
    ("stockbrain.instruments", "instruments", "instruments"),
    ("stockbrain.market_data", "alpaca_market_data", "market_data"),
    ("stockbrain.fx", "fx", "fx"),
    ("stockbrain.risk", "risk", "risk"),
    ("stockbrain.proposals", "proposals", "proposals"),
    ("stockbrain.execution", "execution", "execution"),
    ("stockbrain.broker", "trading212", "broker"),
    ("stockbrain.telegram", "telegram", "notifications"),
    ("stockbrain.observability.alerts", "alerts", "notifications"),
    ("stockbrain.observability", "observability", "observability"),
    ("stockbrain.jobs", "jobs", "jobs"),
    ("stockbrain.db", "postgres", "database"),
    ("stockbrain.api", "api", "api"),
    ("stockbrain.control", "control", "system"),
    ("stockbrain.main", "app", "system"),
    ("stockbrain.startup", "app", "system"),
    ("stockbrain.services", "app", "system"),
    ("stockbrain.config", "config", "system"),
    ("uvicorn", "http", "api"),
    ("sqlalchemy", "postgres", "database"),
    ("httpx", "http", "api"),
)

#: Values of ``provider=`` that name a real service.  A log line that binds one
#: is trusted over its module, because ``stockbrain.jobs.handlers`` logging
#: ``provider="brave"`` is a Brave event that happens to be raised by the job
#: runner.
_PROVIDER_FIELDS: tuple[str, ...] = ("provider", "service")

#: Keys carried on almost every line by the logging framework itself.  They are
#: promoted to real columns or dropped rather than repeated inside ``fields``.
_STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {"event", "level", "logger", "timestamp", "logger_name", "_record", "_from_structlog"}
)

#: A single field value is truncated to this many characters, and an entry keeps
#: at most this many fields.  Both exist so one pathological log call cannot
#: consume the whole buffer's memory budget.
_MAX_FIELD_CHARS = 400
_MAX_FIELDS = 40
_MAX_MESSAGE_CHARS = 2000


def service_for_logger(logger_name: str) -> tuple[str, str]:
    """Resolve a logger name to its ``(service, category)`` pair."""
    best: tuple[str, str, str] | None = None
    for prefix, service, category in _MODULE_SERVICES:
        matched = logger_name == prefix or logger_name.startswith(prefix + ".")
        if matched and (best is None or len(prefix) > len(best[0])):
            best = (prefix, service, category)
    if best is None:
        return ("app", "system")
    return (best[1], best[2])


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One captured structured log event.

    ``sequence`` is a process-local monotonic counter.  It is what the GUI pages
    on: an index into a ring buffer is meaningless once entries are evicted, and
    two events can share a timestamp.
    """

    sequence: int
    timestamp: dt.datetime
    level: str
    severity: int
    logger: str
    event: str
    service: str
    category: str
    message: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def haystack(self) -> str:
        """Everything free-text search looks at, lowercased once per query."""
        parts = [self.event, self.logger, self.service, self.message]
        parts.extend(f"{key}={value}" for key, value in self.fields.items())
        return " ".join(parts).lower()


@dataclass(frozen=True, slots=True)
class LogQuery:
    """A bounded request for entries.  Every field narrows; none widens."""

    min_level: str | None = None
    services: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    search: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class LogFacets:
    """What the buffer currently *contains*, so the filter UI offers only that."""

    services: dict[str, int]
    categories: dict[str, int]
    levels: dict[str, int]


@dataclass(frozen=True, slots=True)
class LogQueryResult:
    entries: tuple[LogEntry, ...]
    total: int
    limit: int
    offset: int
    capacity: int
    stored: int
    dropped: int
    captured_since: dt.datetime | None
    oldest_at: dt.datetime | None
    newest_at: dt.datetime | None
    min_captured_level: str


class LogBuffer:
    """A fixed-size, thread-safe ring of the most recent log entries.

    Thread-safe rather than asyncio-safe: log calls arrive from the event loop,
    from ``run_in_executor`` work and from uvicorn's own threads, so a plain
    ``asyncio.Lock`` would not actually be held by every writer.
    """

    def __init__(self, capacity: int, *, min_level: str = "info") -> None:
        self._capacity = max(0, capacity)
        self._entries: deque[LogEntry] = deque(maxlen=self._capacity or 1)
        self._lock = threading.Lock()
        self._sequence = 0
        self._dropped = 0
        self._started_at = utcnow()
        self._min_severity = LEVEL_SEVERITY.get(min_level.lower(), logging.INFO)
        self._min_level = min_level.lower()

    # ------------------------------------------------------------------
    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def enabled(self) -> bool:
        return self._capacity > 0

    @property
    def min_captured_level(self) -> str:
        return self._min_level

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sequence = 0
            self._dropped = 0
            self._started_at = utcnow()

    # ------------------------------------------------------------------
    def append(self, entry: LogEntry) -> None:
        if not self._capacity:
            return
        with self._lock:
            if len(self._entries) == self._capacity:
                self._dropped += 1
            self._entries.append(entry)

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    # ------------------------------------------------------------------
    def query(self, request: LogQuery) -> LogQueryResult:
        """Filter newest-first and return one page.

        The whole buffer is scanned.  At the configured ceiling of a few
        thousand entries that is microseconds, and the alternative -- per-field
        indexes kept in sync on every log call -- would put real work on the
        logging path to speed up a page nobody loads in a loop.
        """
        with self._lock:
            snapshot = list(self._entries)
            capacity = self._capacity
            dropped = self._dropped
            started_at = self._started_at

        matches = [entry for entry in snapshot if _matches(entry, request)]
        matches.reverse()
        offset = max(0, request.offset)
        limit = max(1, request.limit)
        page = matches[offset : offset + limit]
        return LogQueryResult(
            entries=tuple(page),
            total=len(matches),
            limit=limit,
            offset=offset,
            capacity=capacity,
            stored=len(snapshot),
            dropped=dropped,
            captured_since=started_at,
            oldest_at=snapshot[0].timestamp if snapshot else None,
            newest_at=snapshot[-1].timestamp if snapshot else None,
            min_captured_level=self._min_level,
        )

    def facets(self) -> LogFacets:
        with self._lock:
            snapshot = list(self._entries)
        return LogFacets(
            services=dict(Counter(entry.service for entry in snapshot).most_common()),
            categories=dict(Counter(entry.category for entry in snapshot).most_common()),
            levels=dict(Counter(entry.level for entry in snapshot).most_common()),
        )

    def severity_at_least(self, level: str) -> bool:
        """Whether an event at ``level`` is worth building an entry for."""
        return LEVEL_SEVERITY.get(level.lower(), logging.INFO) >= self._min_severity


def _matches(entry: LogEntry, request: LogQuery) -> bool:
    if request.min_level is not None:
        floor = LEVEL_SEVERITY.get(request.min_level.lower())
        if floor is not None and entry.severity < floor:
            return False
    if request.services and entry.service not in request.services:
        return False
    if request.categories and entry.category not in request.categories:
        return False
    if request.since is not None and entry.timestamp < request.since:
        return False
    if request.until is not None and entry.timestamp > request.until:
        return False
    return not (request.search and request.search.lower() not in entry.haystack)


class LogCaptureProcessor:
    """structlog processor that copies each event into a :class:`LogBuffer`.

    Installed as the **last** processor in the shared chain so it sees the fully
    enriched event: the level, the logger name, the ISO timestamp and, where one
    happened, the rendered exception.  It scrubs that dictionary itself rather
    than trusting the earlier pass, because ``format_exc_info`` runs *after* the
    scrubber and a traceback can carry a credential in an exception message.

    Returns its input unchanged.  A processor that dropped or rewrote events
    would make the buffer and stdout disagree, and stdout is the thing an
    operator falls back to when this page is the thing that is broken.
    """

    def __init__(self, buffer: LogBuffer, scrubber: Any = None) -> None:
        self._buffer = buffer
        self._scrubber = scrubber

    def __call__(self, _logger: object, _name: str, event_dict: EventDict) -> EventDict:
        if not self._buffer.enabled:
            return event_dict
        level = str(event_dict.get("level") or _name or "info").lower()
        if not self._buffer.severity_at_least(level):
            return event_dict
        # Capture must never be the reason a log line is lost, so every failure
        # here is swallowed: the event still reaches stdout, which is the copy
        # an operator falls back to when this page is the thing that is broken.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self._buffer.append(self._build(dict(event_dict), level))
        return event_dict

    # ------------------------------------------------------------------
    def _build(self, raw: dict[str, Any], level: str) -> LogEntry:
        if self._scrubber is not None:
            scrubbed = self._scrubber(None, level, raw)
            raw = dict(scrubbed)

        logger_name = str(raw.get("logger") or raw.get("logger_name") or "")
        event = str(raw.get("event") or "")
        service, category = service_for_logger(logger_name)
        for key in _PROVIDER_FIELDS:
            named = raw.get(key)
            if (
                isinstance(named, str)
                and named
                and any(named == candidate for _, candidate, _ in _MODULE_SERVICES)
            ):
                service = named
                break

        fields: dict[str, str] = {}
        exception = ""
        for key, value in raw.items():
            if key in _STRUCTURAL_KEYS:
                continue
            if key == "exception":
                exception = str(value)[:_MAX_MESSAGE_CHARS]
                continue
            if len(fields) >= _MAX_FIELDS:
                break
            fields[str(key)] = _render(value)

        return LogEntry(
            sequence=self._buffer.next_sequence(),
            timestamp=_timestamp(raw.get("timestamp")),
            level=level,
            severity=LEVEL_SEVERITY.get(level, logging.INFO),
            logger=logger_name,
            event=event[:_MAX_FIELD_CHARS],
            service=service,
            category=category,
            message=exception,
            fields=fields,
        )


def _render(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) > _MAX_FIELD_CHARS:
        return text[: _MAX_FIELD_CHARS - 1] + "…"
    return text


def _timestamp(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utcnow()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return utcnow()


def normalise_levels(values: Iterable[str] | None) -> tuple[str, ...]:
    """Lowercase, de-duplicate and drop unknown level names."""
    if not values:
        return ()
    seen: list[str] = []
    for value in values:
        lowered = value.strip().lower()
        if lowered in LEVEL_SEVERITY and lowered not in seen:
            seen.append(lowered)
    return tuple(seen)


def csv_tuple(value: str | None, *, limit: int = 20) -> tuple[str, ...]:
    """Split a comma-separated filter parameter into a bounded tuple."""
    if not value:
        return ()
    parts: list[str] = []
    for chunk in value.split(","):
        text = chunk.strip()
        if text and text not in parts:
            parts.append(text)
        if len(parts) >= limit:
            break
    return tuple(parts)


def known_services() -> Sequence[str]:
    """Every service key this module can produce, for API documentation."""
    return sorted({service for _, service, _ in _MODULE_SERVICES})
