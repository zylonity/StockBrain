"""Metrics registry tests."""

from __future__ import annotations

import pytest

from stockbrain.observability.metrics import MetricsRegistry


def test_counter_accumulates_per_label_set() -> None:
    registry = MetricsRegistry()
    registry.counter("things_total", "Things.")
    registry.inc("things_total", labels={"kind": "a"})
    registry.inc("things_total", labels={"kind": "a"})
    registry.inc("things_total", labels={"kind": "b"})

    assert registry.get("things_total", {"kind": "a"}) == 2.0
    assert registry.get("things_total", {"kind": "b"}) == 1.0
    assert registry.get("things_total") == 0.0


def test_label_order_does_not_create_separate_series() -> None:
    registry = MetricsRegistry()
    registry.inc("t", labels={"a": "1", "b": "2"})
    registry.inc("t", labels={"b": "2", "a": "1"})
    assert registry.get("t", {"a": "1", "b": "2"}) == 2.0


def test_render_emits_prometheus_text_format() -> None:
    registry = MetricsRegistry()
    registry.counter("requests_total", "Requests handled.")
    registry.inc("requests_total", labels={"status": "200"})
    rendered = registry.render()

    assert "# HELP requests_total Requests handled." in rendered
    assert "# TYPE requests_total counter" in rendered
    assert 'requests_total{status="200"} 1.0' in rendered
    assert rendered.endswith("\n")


def test_declared_but_unused_metric_renders_zero() -> None:
    registry = MetricsRegistry()
    registry.counter("never_used_total", "Unused.")
    assert "never_used_total 0" in registry.render()


def test_label_values_are_escaped() -> None:
    registry = MetricsRegistry()
    registry.inc("t", labels={"detail": 'a "quoted" \\ value'})
    assert r'detail="a \"quoted\" \\ value"' in registry.render()


def test_redeclaring_with_a_different_type_is_rejected() -> None:
    registry = MetricsRegistry()
    registry.counter("x")
    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("x")


def test_gauge_is_set_not_accumulated() -> None:
    registry = MetricsRegistry()
    registry.gauge("queue_depth")
    registry.set("queue_depth", 5)
    registry.set("queue_depth", 2)
    assert registry.get("queue_depth") == 2.0
