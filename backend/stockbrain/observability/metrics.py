"""Minimal in-process metrics with Prometheus text exposition.

Deliberately hand-rolled rather than pulling in a client library: StockBrain
needs counters and gauges only, and a Prometheus server may never be deployed.
Metric names follow the specification's list so a future scraper needs no
translation layer.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = ["METRICS", "MetricsRegistry"]

_Labels = tuple[tuple[str, str], ...]


def _normalise(labels: Mapping[str, str] | None) -> _Labels:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass(slots=True)
class _Metric:
    name: str
    help_text: str
    metric_type: str
    values: dict[_Labels, float] = field(default_factory=dict)


class MetricsRegistry:
    """Thread-safe registry.  Counters only ever increase; gauges are set."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str = "") -> None:
        self._declare(name, help_text, "counter")

    def gauge(self, name: str, help_text: str = "") -> None:
        self._declare(name, help_text, "gauge")

    def _declare(self, name: str, help_text: str, metric_type: str) -> None:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                self._metrics[name] = _Metric(name, help_text, metric_type)
            elif existing.metric_type != metric_type:
                raise ValueError(
                    f"metric {name!r} already registered as {existing.metric_type}, "
                    f"cannot redeclare as {metric_type}"
                )

    def inc(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        key = _normalise(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = _Metric(name, "", "counter")
                self._metrics[name] = metric
            metric.values[key] = metric.values.get(key, 0.0) + value

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        key = _normalise(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = _Metric(name, "", "gauge")
                self._metrics[name] = metric
            metric.values[key] = value

    def get(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                return 0.0
            return metric.values.get(_normalise(labels), 0.0)

    def reset(self) -> None:
        """Used by tests only."""
        with self._lock:
            for metric in self._metrics.values():
                metric.values.clear()

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for metric in sorted(self._metrics.values(), key=lambda m: m.name):
                if metric.help_text:
                    lines.append(f"# HELP {metric.name} {metric.help_text}")
                lines.append(f"# TYPE {metric.name} {metric.metric_type}")
                if not metric.values:
                    lines.append(f"{metric.name} 0")
                    continue
                for labels, value in sorted(metric.values.items()):
                    rendered = _render_labels(labels)
                    lines.append(f"{metric.name}{rendered} {value!r}")
        return "\n".join(lines) + "\n"


def _render_labels(labels: Sequence[tuple[str, str]]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + inner + "}"


METRICS = MetricsRegistry()

# Declared up front so /metrics reports zeros rather than omitting a series
# entirely before the first occurrence (spec section 29).
for _name, _help in (
    ("stockbrain_news_items_received_total", "Raw news items received from providers."),
    ("stockbrain_events_created_total", "Canonical events created."),
    ("stockbrain_events_deduped_total", "Incoming sources merged into an existing event."),
    ("stockbrain_classifier_calls_total", "Event classifier invocations."),
    ("stockbrain_classifier_failures_total", "Event classifier failures."),
    ("stockbrain_research_runs_total", "Research runs started."),
    ("stockbrain_llm_input_tokens_total", "LLM input tokens consumed."),
    ("stockbrain_llm_output_tokens_total", "LLM output tokens produced."),
    ("stockbrain_llm_cost_estimate_usd", "Estimated LLM spend in USD."),
    ("stockbrain_firecrawl_credits_total", "Firecrawl credits consumed."),
    ("stockbrain_proposals_created_total", "Trade proposals created."),
    ("stockbrain_proposals_approved_total", "Trade proposals approved by a human."),
    ("stockbrain_proposals_rejected_total", "Trade proposals rejected by a human."),
    ("stockbrain_broker_orders_total", "Orders submitted to a broker."),
    (
        "stockbrain_broker_ambiguous_submissions_total",
        "Order submissions whose outcome was not definitively known.",
    ),
    ("stockbrain_provider_errors_total", "Errors returned by external providers."),
    ("stockbrain_jobs_processed_total", "Background jobs processed."),
    ("stockbrain_http_requests_total", "HTTP requests handled."),
):
    METRICS.counter(_name, _help)

for _name, _help in (
    ("stockbrain_up", "1 when the application process is running."),
    ("stockbrain_provider_status", "Provider health as a severity ordinal (0 best)."),
    ("stockbrain_jobs_pending", "Jobs currently pending in the queue."),
):
    METRICS.gauge(_name, _help)

METRICS.set("stockbrain_up", 1.0)
