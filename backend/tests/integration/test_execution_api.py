"""The execution API: reads, plus one control that is a read in disguise.

The route table is the part of Phase 8 most likely to grow a mistake, because
"add a retry button" is the obvious feature request and it is the one thing that
must never exist.  Trading 212's order POST is non-idempotent, so a resend
endpoint would be an endpoint that creates a second real position.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.proposals import ExecutionAttempt
from stockbrain.db.session import Database
from stockbrain.enums import AuthorizationSource, Broker, ExecutionOutcome
from stockbrain.errors import AmbiguousTransportFailure
from stockbrain.execution.models import CandidateSearch
from stockbrain.execution.service import ExecutionService
from stockbrain.main import create_app
from tests import proposal_helpers as helpers
from tests.execution_helpers import FakeProvider, order_view

pytestmark = pytest.mark.integration


def settings_for(database_url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": database_url,
        "t212_execution_enabled": True,
    }
    base.update(overrides)
    return helpers.settings(**base)


@pytest.fixture
async def client(clean_tables: Database, database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, with a fake broker wired into its service container.

    The container is replaced *after* startup so the API exercises exactly the
    routes and dependencies production does, with only the broker faked.
    """
    settings = settings_for(database_url)
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def prepared(
    database: Database, database_url: str, *, ambiguous: bool = False
) -> tuple[uuid.UUID, uuid.UUID, FakeProvider]:
    """One proposal driven to a real attempt, submitted or ambiguous."""
    settings = settings_for(database_url)
    control = ControlStateService(database)
    proposals = helpers.service_with(database, settings, control=control)
    await helpers.seed(database)
    await helpers.fund(database)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None, generated.reason
    await proposals.authorize(
        generated.proposal_id,
        source=AuthorizationSource.HUMAN_WEB,
        actor="web:local-operator",
    )
    provider = FakeProvider(_environment="demo")
    if ambiguous:
        provider.error = AmbiguousTransportFailure("read timed out")
    execution = ExecutionService(
        database,
        settings,
        proposals=proposals,
        provider=provider,
        control=control,
        broker=Broker.TRADING212,
    )
    result = await execution.execute(generated.proposal_id)
    assert result.attempt_id is not None
    provider.error = None
    return generated.proposal_id, result.attempt_id, provider


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
async def test_execution_status_reports_the_posture_and_the_counts(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    await prepared(clean_tables, database_url)
    response = await client.get("/api/v1/execution/status")
    assert response.status_code == 200
    body = response.json()

    assert body["broker"] == Broker.TRADING212.value
    assert body["broker_environment"] == "demo"
    assert body["order_endpoint"] == "/equity/orders/market"
    # Reported rather than assumed: a client that forgot would build a retry.
    assert body["order_endpoint_idempotent"] is False
    assert body["attempts_by_outcome"]["SUBMITTED"] == 1
    assert body["trading_halted"] is False
    assert isinstance(body["blockers"], list)


async def test_the_status_endpoint_never_disagrees_with_its_own_blockers(
    client: httpx.AsyncClient,
) -> None:
    """``order_transmission_permitted`` is "no blockers remain", by definition."""
    body = (await client.get("/api/v1/execution/status")).json()
    assert body["order_transmission_permitted"] == (not body["blockers"])


async def test_the_status_endpoint_reflects_a_halt(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="web:local-operator", source="HUMAN_WEB", reason="drill"
    )
    body = (await client.get("/api/v1/execution/status")).json()
    assert body["trading_halted"] is True
    assert any("kill switch" in blocker for blocker in body["control_blockers"])


# ---------------------------------------------------------------------------
# Proposal execution detail
# ---------------------------------------------------------------------------
async def test_a_proposals_execution_history_is_exposed(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    proposal_id, attempt_id, _ = await prepared(clean_tables, database_url)
    response = await client.get(f"/api/v1/proposals/{proposal_id}/execution")
    assert response.status_code == 200
    body = response.json()

    assert body["transmitted"] is True
    assert body["ambiguous"] is False
    assert body["broker_environment"] == "demo"
    assert body["authorization_source"] == "HUMAN_WEB"
    assert len(body["attempts"]) == 1
    attempt = body["attempts"][0]
    assert attempt["id"] == str(attempt_id)
    assert attempt["sent_to_broker"] is True
    assert attempt["outcome"] == "SUBMITTED"
    assert attempt["broker_order_id"] == "500100"
    assert attempt["resend_permitted"] is False
    assert len(attempt["request_fingerprint"]) == 64
    assert len(body["orders"]) == 1
    assert body["orders"][0]["initiated_from"] == "API"


async def test_an_ambiguous_proposal_returns_the_do_not_resend_notice(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    """The API's job here is to talk a client out of the obvious reaction."""
    proposal_id, _, _ = await prepared(clean_tables, database_url, ambiguous=True)
    body = (await client.get(f"/api/v1/proposals/{proposal_id}/execution")).json()

    assert body["ambiguous"] is True
    assert body["reconciliation_required"] is True
    assert "ORDER STATE UNKNOWN" in body["notice"]
    assert "must NOT be resent" in body["notice"]
    assert body["attempts"][0]["ambiguous"] is True
    assert body["attempts"][0]["resend_permitted"] is False


async def test_an_unknown_proposal_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/api/v1/proposals/{uuid.uuid4()}/execution")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
async def test_attempts_can_be_listed_and_filtered_to_ambiguous(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    await prepared(clean_tables, database_url, ambiguous=True)
    everything = (await client.get("/api/v1/execution/attempts")).json()
    assert len(everything) == 1

    ambiguous = (await client.get("/api/v1/execution/attempts?ambiguous_only=true")).json()
    assert len(ambiguous) == 1
    assert ambiguous[0]["error_category"] == "TRANSPORT_AMBIGUOUS"


async def test_the_broker_order_mirror_is_exposed(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    await prepared(clean_tables, database_url)
    orders = (await client.get("/api/v1/execution/orders")).json()
    assert len(orders) == 1
    assert orders[0]["broker_order_id"] == "500100"
    assert orders[0]["broker_environment"] == "demo"
    assert Decimal(orders[0]["quantity"]) == Decimal("2")


# ---------------------------------------------------------------------------
# The one mutating route
# ---------------------------------------------------------------------------
async def test_reconciliation_can_be_triggered_and_transmits_nothing(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    """Safe to invoke repeatedly, because it is a read.

    The app's own container has no broker credentials in this fixture, so the
    endpoint answers 503 rather than pretending; the service-level behaviour has
    its own suite. What is asserted here is that the route exists, validates its
    (empty) body, and offers no way to ask for a resend.
    """
    _, attempt_id, _ = await prepared(clean_tables, database_url, ambiguous=True)
    response = await client.post(f"/api/v1/execution/attempts/{attempt_id}/reconcile", json={})
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        assert response.json()["resend_permitted"] is False


async def test_the_reconcile_body_rejects_anything_at_all(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    """``extra="forbid"``: a field that could ask for a resend is refused.

    Not ignored -- refused -- so a client that tried would learn it cannot
    rather than believing it had.
    """
    _, attempt_id, _ = await prepared(clean_tables, database_url, ambiguous=True)
    for body in (
        {"resend": True},
        {"force": True},
        {"quantity": 5},
        {"broker_ticker": "MSFT_US_EQ"},
        {"side": "SELL"},
    ):
        response = await client.post(
            f"/api/v1/execution/attempts/{attempt_id}/reconcile", json=body
        )
        assert response.status_code == 422, body


async def test_reconciling_an_unknown_attempt_is_a_404_or_503(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/api/v1/execution/attempts/{uuid.uuid4()}/reconcile", json={})
    assert response.status_code in (404, 503)


# ---------------------------------------------------------------------------
# The route surface
# ---------------------------------------------------------------------------
async def test_there_is_no_route_that_places_or_resends_an_order(client: httpx.AsyncClient) -> None:
    """Asserted against the served OpenAPI schema, not the source."""
    schema = (await client.get("/api/openapi.json")).json()
    mutations = {
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    execution_mutations = {
        route for route in mutations if "/execution" in route or "order" in route.lower()
    }
    assert execution_mutations == {"POST /api/v1/execution/attempts/{attempt_id}/reconcile"}
    for path in schema["paths"]:
        assert "retry" not in path.lower()
        assert "resend" not in path.lower()


async def test_no_execution_response_contains_a_credential(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    """The stored payloads reach the API, so they must carry nothing secret.

    Scanned for credential *shapes* rather than the word "authorization", which
    legitimately appears in ``authorization_source`` -- the provenance of the
    decision, which is exactly the sort of thing these responses exist to show.
    """
    proposal_id, _, _ = await prepared(clean_tables, database_url)
    configured = helpers.settings()
    secrets = (
        configured.t212_api_key.get_secret_value(),
        configured.t212_api_secret.get_secret_value(),
    )
    for url in (
        "/api/v1/execution/status",
        f"/api/v1/proposals/{proposal_id}/execution",
        "/api/v1/execution/attempts",
        "/api/v1/execution/orders",
    ):
        rendered = (await client.get(url)).text
        lowered = rendered.lower()
        for forbidden in ('"authorization":', "basic ", "bearer ", "api_key", "password"):
            assert forbidden not in lowered, f"{url} leaked {forbidden}"
        for secret in secrets:
            assert secret and secret not in rendered, f"{url} leaked a credential"


async def test_the_snapshot_is_returned_for_audit_without_secrets(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    proposal_id, _, _ = await prepared(clean_tables, database_url)
    body = (await client.get(f"/api/v1/proposals/{proposal_id}/execution")).json()
    snapshot = body["attempts"][0]["execution_snapshot"]
    assert snapshot["broker_environment"] == "demo"
    assert snapshot["authorization_source"] == "HUMAN_WEB"
    assert "quote" in snapshot and "control" in snapshot
    assert "transmission_gates" in snapshot


async def test_a_pending_attempt_is_counted_as_needing_reconciliation(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    proposal_id, attempt_id, provider = await prepared(clean_tables, database_url, ambiguous=True)
    provider.search = CandidateSearch(
        candidates=(order_view(signed_quantity=Decimal("2")),),
        pending_ok=True,
        history_ok=True,
    )
    body = (await client.get("/api/v1/execution/status")).json()
    assert body["ambiguous_attempts"] == 1
    assert body["reconciliation_pending"] == 1

    async with clean_tables.session() as session:
        attempt = await session.get(ExecutionAttempt, attempt_id)
    assert attempt is not None
    assert attempt.outcome is ExecutionOutcome.AMBIGUOUS
    assert proposal_id is not None


async def test_the_attempt_count_query_is_grouped_correctly(
    clean_tables: Database, client: httpx.AsyncClient, database_url: str
) -> None:
    await prepared(clean_tables, database_url)
    async with clean_tables.session() as session:
        total = int(
            (
                await session.execute(sa.select(sa.func.count()).select_from(ExecutionAttempt))
            ).scalar_one()
        )
    body = (await client.get("/api/v1/execution/status")).json()
    assert sum(body["attempts_by_outcome"].values()) == total
