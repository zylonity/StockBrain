"""Provider-health aggregation tests.

The rule under test: an external outage degrades its own subsystem and nothing
else.  Only PostgreSQL can make the whole application unhealthy.
"""

from __future__ import annotations

from stockbrain.config import Settings
from stockbrain.enums import ProviderStatus as P
from stockbrain.observability.health import (
    ProviderHealthRegistry,
    ProviderName,
    Subsystem,
    aggregate_status,
)
from stockbrain.startup import register_static_provider_states


def _healthy_registry() -> ProviderHealthRegistry:
    registry = ProviderHealthRegistry()
    for provider in ProviderName:
        registry.record(provider, P.HEALTHY)
    return registry


def test_redundant_subsystem_degrades_rather_than_fails() -> None:
    assert aggregate_status([P.HEALTHY, P.DOWN], require_all=False) is P.DEGRADED
    assert aggregate_status([P.HEALTHY, P.HEALTHY], require_all=False) is P.HEALTHY
    assert aggregate_status([P.DOWN, P.DOWN], require_all=False) is P.DOWN


def test_disabled_providers_do_not_count_as_faults() -> None:
    assert aggregate_status([P.HEALTHY, P.DISABLED], require_all=False) is P.HEALTHY
    assert aggregate_status([P.DISABLED, P.DISABLED], require_all=False) is P.DISABLED


def test_required_subsystem_takes_the_worst_status() -> None:
    assert aggregate_status([P.HEALTHY, P.DEGRADED], require_all=True) is P.DEGRADED
    assert aggregate_status([P.DEGRADED, P.DOWN], require_all=True) is P.DOWN


def test_alpaca_news_outage_degrades_discovery_only() -> None:
    registry = _healthy_registry()
    registry.record(ProviderName.ALPACA_NEWS, P.DOWN, detail="entitlement missing")

    assert registry.subsystem_status(Subsystem.DISCOVERY) is P.DEGRADED
    assert registry.subsystem_status(Subsystem.RESEARCH) is P.HEALTHY
    assert registry.subsystem_status(Subsystem.EXECUTION) is P.HEALTHY
    assert registry.overall_status() is P.DEGRADED


def test_broker_outage_leaves_research_healthy() -> None:
    registry = _healthy_registry()
    registry.record(ProviderName.TRADING212, P.DOWN)

    assert registry.subsystem_status(Subsystem.EXECUTION) is P.DOWN
    assert registry.subsystem_status(Subsystem.RESEARCH) is P.HEALTHY
    assert registry.subsystem_status(Subsystem.DATABASE) is P.HEALTHY


def test_database_outage_makes_the_application_down() -> None:
    registry = _healthy_registry()
    registry.record(ProviderName.POSTGRES, P.DOWN)
    assert registry.overall_status() is P.DOWN


def test_consecutive_failures_reset_on_recovery() -> None:
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.FIRECRAWL, P.DOWN)
    registry.record(ProviderName.FIRECRAWL, P.DOWN)
    assert registry.get(ProviderName.FIRECRAWL).consecutive_failures == 2

    registry.record(ProviderName.FIRECRAWL, P.HEALTHY)
    state = registry.get(ProviderName.FIRECRAWL)
    assert state.consecutive_failures == 0
    assert state.last_ok_at is not None


def test_unconfigured_providers_are_marked_disabled_not_unknown() -> None:
    settings = Settings(app_env="test")
    registry = ProviderHealthRegistry()
    register_static_provider_states(settings, registry)

    for provider in (
        ProviderName.LLM,
        ProviderName.ALPACA_NEWS,
        ProviderName.FIRECRAWL,
        ProviderName.FRED,
        ProviderName.TRADING212,
        ProviderName.TELEGRAM,
    ):
        assert registry.get(provider).status is P.DISABLED
        assert registry.get(provider).detail


def test_telegram_with_token_but_empty_allowlist_is_disabled() -> None:
    """An empty allowlist must authorise nobody, so the bot stays off."""
    settings = Settings(
        app_env="test",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_allowed_user_ids=[],
    )
    registry = ProviderHealthRegistry()
    register_static_provider_states(settings, registry)

    state = registry.get(ProviderName.TELEGRAM)
    assert state.status is P.DISABLED
    assert state.detail is not None
    assert "authorises nobody" in state.detail
