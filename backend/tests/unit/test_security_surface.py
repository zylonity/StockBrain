"""Security properties of the HTTP and LLM surface.

Phase 6 deliberately breaks the old "zero state-changing routes" property: the
approve, reject and cancel endpoints mutate StockBrain's own state. What must
*not* change is the invariant that actually protects money, so the assertions
are narrowed rather than deleted:

1. **Zero broker order/execution routes.** Every state-changing route mutates
   StockBrain state only; no API path reaches a Trading 212 mutation, and no
   order/cancel/modify method exists anywhere to be reached.
2. **No LLM path acquires broker capability.** The provider interface is
   text-in / text-out, the classifier hint reaches instrument resolution as
   data rather than as an identity, and no model-supplied number can enter
   sizing or lift a deterministic block.
3. **Secrets stay redacted** in logs, in exception messages and in API output.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from stockbrain.api.routes import instruments as instrument_routes
from stockbrain.broker import instrument_sync, trading212_metadata
from stockbrain.config import Settings
from stockbrain.instruments import resolver
from stockbrain.instruments import service as resolution_service
from stockbrain.llm.base import LlmProvider
from stockbrain.logging import REDACTED, SecretScrubber
from stockbrain.main import create_app
from stockbrain.market_data import alpaca

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Words that would name a *broker* action rather than an internal one. An
#: approval endpoint is legitimate in this phase; an order endpoint is not, and
#: will not be until Phase 8 deliberately adds one behind the four live gates.
_BROKER_ACTION_WORDS = ("order", "execute", "submit", "broker-order", "trade212", "t212")

#: The complete set of state-changing routes this phase is allowed to have.
#: New entries here are a decision, not an accident.
#:
#: Phase 7 adds three, and all three move in the *safe* direction: they halt
#: proposal generation and authorization, or lift a halt. None of them touches a
#: proposal, a quantity, a price or a broker; the kill switch in particular
#: closes no position and cancels no order, which the module scan below proves
#: structurally rather than by assertion of intent.
_EXPECTED_MUTATIONS = {
    "POST /api/v1/proposals/{proposal_id}/approve",
    "POST /api/v1/proposals/{proposal_id}/reject",
    "POST /api/v1/proposals/{proposal_id}/cancel",
    "POST /api/v1/system/pause",
    "POST /api/v1/system/resume",
    "POST /api/v1/system/kill-switch",
}

#: Every Trading 212 path that would change broker state.
_T212_MUTATION_PATHS = (
    "/equity/orders",
    "/equity/orders/market",
    "/equity/orders/limit",
    "/equity/orders/stop",
    "/equity/orders/stop_limit",
    "/equity/pies",
)


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


def test_the_only_state_changing_routes_are_the_expected_internal_ones() -> None:
    """Cross-checked against the OpenAPI schema.

    The route walk and the schema are independent views of the same surface, so
    a mutating route nobody declared here would have to hide from both.
    """
    schema = create_app(_settings()).openapi()
    documented = {
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in _MUTATION_METHODS
    }
    assert documented == _EXPECTED_MUTATIONS

    walked = {
        f"{method} {route.path}"
        for route in _api_routes()
        for method in (route.methods or set())
        if method in _MUTATION_METHODS
    }
    assert walked == _EXPECTED_MUTATIONS


def test_no_route_path_names_a_broker_order_or_execution() -> None:
    """Approving is an internal act; ordering is not, and has no route."""
    offenders = [
        route.path
        for route in _api_routes()
        if any(word in route.path.lower() for word in _BROKER_ACTION_WORDS)
    ]
    assert offenders == []


def test_no_api_path_can_reach_a_trading212_mutation() -> None:
    """Structural: the route modules must not name a broker mutation path.

    An approval endpoint is only safe while approving cannot reach an order, so
    this asserts the absence of the path string rather than trusting that no
    call site happens to use it today.
    """
    import stockbrain.api.routes.proposals as proposal_routes
    import stockbrain.api.routes.system as system_routes

    for module in (proposal_routes, instrument_routes, system_routes):
        source = inspect.getsource(module)
        for path in _T212_MUTATION_PATHS:
            assert path not in source
        assert "place_order" not in source
        assert "place_market_order" not in source


def test_no_trading212_order_cancel_or_modify_method_exists_anywhere() -> None:
    """The capability audit: a method that does not exist cannot be called.

    Every module under ``stockbrain`` is scanned, not only the broker package,
    because the guarantee is about the process rather than about one file.
    """
    root = Path(inspect.getfile(create_app)).resolve().parent
    forbidden = (
        "place_order",
        "place_market_order",
        "place_limit_order",
        "cancel_order",
        "modify_order",
        "amend_order",
        '"/equity/orders',
        "'/equity/orders",
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")
    assert offenders == []


def test_the_broker_clients_expose_no_mutation_method() -> None:
    """Both Trading 212 clients are read-only by their public surface."""
    from stockbrain.broker.trading212_account import Trading212AccountClient

    for client in (trading212_metadata.Trading212MetadataClient, Trading212AccountClient):
        methods = {name for name, _ in inspect.getmembers(client) if not name.startswith("_")}
        assert not methods & {
            "place_order",
            "place_market_order",
            "cancel_order",
            "modify_order",
            "post",
            "delete",
        }
        assert {name for name in methods if name.startswith("fetch_")} <= {
            "fetch_instruments",
            "fetch_exchanges",
            "fetch_account_summary",
            "fetch_positions",
        }


def test_the_phase_four_inspection_routes_are_still_reads() -> None:
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
        if route.path.startswith("/api/v1/instruments") or route.path in {
            "/api/v1/aliases",
            "/api/v1/market-data/health",
        }:
            assert (route.methods or set()) <= {"GET", "HEAD", "OPTIONS"}


def test_the_new_proposal_routes_are_exposed_for_inspection() -> None:
    paths = {route.path for route in _api_routes()}
    for path in (
        "/api/v1/proposals",
        "/api/v1/proposals/policy",
        "/api/v1/proposals/{proposal_id}",
        "/api/v1/proposals/{proposal_id}/risk",
        "/api/v1/risk/evaluations",
    ):
        assert path in paths, f"{path} should be exposed"


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
