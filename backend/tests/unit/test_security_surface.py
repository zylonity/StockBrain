"""Security properties of the Phase 4 surface.

Three invariants, each stated as a test so a later refactor has to argue with
one rather than quietly step past it:

1. **There are still zero state-changing HTTP routes**, and in particular no
   route that could place, amend or cancal a broker order.
2. **No LLM path acquires broker capability.** The provider interface is
   text-in / text-out, and the classifier hint reaches instrument resolution as
   data, not as an identity.
3. **Secrets stay redacted** in logs, in exception messages and in API output.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from stockbrain.broker import instrument_sync, trading212_metadata
from stockbrain.config import Settings
from stockbrain.instruments import resolver
from stockbrain.instruments import service as resolution_service
from stockbrain.llm.base import LlmProvider
from stockbrain.logging import REDACTED, SecretScrubber
from stockbrain.main import create_app
from stockbrain.market_data import alpaca

#: Anything that would mutate broker state. A route matching one of these
#: verbs, or a path containing one of these words, must not exist yet.
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ORDER_WORDS = ("order", "execute", "submit", "cancel", "approve", "confirm")


def _settings() -> Settings:
    return Settings(app_env="test", stockbrain_secret_key="not-a-real-key")


def _api_routes() -> list[APIRoute]:
    """Every API route, walked recursively.

    FastAPI wraps an included router in a container route, so a flat scan of
    ``app.routes`` silently finds nothing -- which would make every assertion
    below pass vacuously. The recursion is the point of this helper.
    """
    app = create_app(_settings())
    collected: list[APIRoute] = []

    def walk(routes: Iterable[BaseRoute]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                if route.path.startswith("/api"):
                    collected.append(route)
                continue
            # An included router is wrapped in a container route; its own
            # routes hang off `original_router`, and a flat scan misses them.
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", None) or getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    assert collected, "no API routes were discovered; the walk is broken"
    return collected


def test_every_documented_api_operation_is_a_read() -> None:
    """Cross-check against the OpenAPI schema.

    The route walk and the schema are independent views of the same surface; a
    mutation would have to hide from both.
    """
    schema = create_app(_settings()).openapi()
    offenders = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in _MUTATION_METHODS
    ]
    assert offenders == []


def test_there_are_still_zero_state_changing_api_routes() -> None:
    """Phase 4 adds inspection, not action.

    The handoff records this as an intentional property to re-verify whenever
    routes are added; the first state-changing route in this system will be an
    approval endpoint, behind a two-stage human confirmation.
    """
    offenders = [
        f"{sorted(route.methods or set())} {route.path}"
        for route in _api_routes()
        if (route.methods or set()) & _MUTATION_METHODS
    ]
    assert offenders == []


def test_no_route_path_mentions_an_order_or_an_execution() -> None:
    offenders = [
        route.path
        for route in _api_routes()
        if any(word in route.path.lower() for word in _ORDER_WORDS)
    ]
    assert offenders == []


def test_the_new_phase_four_routes_are_all_reads() -> None:
    paths = {route.path for route in _api_routes()}
    for path in (
        "/api/v1/instruments",
        "/api/v1/instruments/resolutions",
        "/api/v1/instruments/sync-status",
        "/api/v1/aliases",
        "/api/v1/market-data/health",
    ):
        assert path in paths, f"{path} should be exposed for inspection"
    for route in _api_routes():
        assert (route.methods or set()) <= {"GET", "HEAD", "OPTIONS"}


# ---------------------------------------------------------------------------
# No LLM path obtains broker capability
# ---------------------------------------------------------------------------
def test_the_llm_provider_interface_offers_no_tools() -> None:
    """A model reached through this interface has nothing to reach for."""
    members = {name for name, _ in inspect.getmembers(LlmProvider) if not name.startswith("_")}
    assert not members & {"tools", "tool_choice", "functions", "call_tool", "bind_tools"}


def test_no_llm_module_can_import_a_broker_client() -> None:
    """Structural, not aspirational: the modules that talk to a model must not
    reference the modules that talk to the broker."""
    import stockbrain.intelligence.classifier as classifier
    import stockbrain.intelligence.service as intelligence_service
    import stockbrain.llm.deepseek as deepseek

    for module in (deepseek, classifier, intelligence_service):
        source = inspect.getsource(module)
        assert "trading212" not in source.lower()
        assert "broker_ticker" not in source
        assert "place_order" not in source


def test_the_resolver_never_writes_a_ticker_hint_as_an_identity() -> None:
    """``broker_instrument_id`` may only ever come from a resolver verdict.

    The resolution service assigns it from ``outcome.broker_instrument_id``,
    which the resolver only ever populates with a ``broker_instruments``
    primary key it selected.
    """
    source = inspect.getsource(resolution_service)
    assert "impact.broker_instrument_id = (" in source
    assert "ticker_hint" in source, "the hint is read as a search key"
    # ...but never assigned to the executable identity.
    assert "broker_instrument_id = impact.ticker_hint" not in source
    assert "broker_ticker=impact.ticker_hint" not in source

    resolver_source = inspect.getsource(resolver)
    assert "BrokerInstrument" in resolver_source
    # Every candidate is built from a database row, never from a request field.
    assert "broker_instrument_id=instrument.id" in resolver_source


def test_the_metadata_and_sync_modules_contain_no_mutation_verb() -> None:
    for module in (trading212_metadata, instrument_sync):
        source = inspect.getsource(module)
        assert "/orders" not in source
        assert "place_order" not in source
        # `request_json("POST"` is how a mutation would be issued.
        assert 'request_json("POST"' not in source
        assert 'request_json("DELETE"' not in source


def test_the_market_data_client_only_reads() -> None:
    source = inspect.getsource(alpaca)
    assert 'request_json("POST"' not in source
    assert "/v2/orders" not in source


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_broker_and_alpaca_credentials_are_secret_str() -> None:
    settings = Settings(
        app_env="test",
        t212_api_key="t212-key-value",
        t212_api_secret="t212-secret-value",
        alpaca_api_key="alpaca-key-value",
        alpaca_api_secret="alpaca-secret-value",
    )
    rendered = repr(settings)
    for secret in (
        "t212-key-value",
        "t212-secret-value",
        "alpaca-key-value",
        "alpaca-secret-value",
    ):
        assert secret not in rendered


def test_a_broker_authorization_header_is_redacted_in_a_log_event() -> None:
    """The Basic credential built for Trading 212 must never reach a log line."""
    event = {
        "event": "provider_call",
        "headers": {"Authorization": "Basic a2V5OnNlY3JldA=="},
        "apca_api_secret_key": "alpaca-secret-value",
    }
    redacted = SecretScrubber(["alpaca-secret-value"])(None, "info", dict(event))
    rendered = repr(redacted)
    assert "a2V5OnNlY3JldA==" not in rendered
    assert "alpaca-secret-value" not in rendered
    assert redacted["headers"]["Authorization"] == REDACTED


def test_the_t212_basic_credential_is_scrubbed_from_a_free_text_message() -> None:
    """Value-based scrubbing catches the credential where no key name gives it away."""
    settings = Settings(
        app_env="test",
        t212_api_key="t212-key-value",
        t212_api_secret="t212-secret-value",
    )
    from stockbrain.logging import _collect_secret_values

    scrubber = SecretScrubber(_collect_secret_values(settings))
    result = scrubber(
        None, "error", {"event": "failed", "detail": "auth failed for t212-key-value"}
    )
    assert "t212-key-value" not in repr(result)


def test_live_provider_tests_are_deselected_by_default() -> None:
    """A plain ``pytest`` must not spend real API calls.

    The ``live`` marker only documented an intention. Nothing enforced it, so
    the live tests were opt-in purely because no credentials were configured --
    and the moment a real key landed in ``.env`` a plain ``pytest`` started
    calling Trading 212 and Alpaca on every run, against endpoints rate-limited
    to one request per fifty seconds.
    """
    import tomllib

    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert '-m "not live"' in addopts
