"""The proposal HTTP surface, and the job that fills it.

These are the first state-changing routes in the system. What they change is
StockBrain's own state; the tests below assert that boundary from both sides --
what a client may send, and what the API can reach.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from asgi_lifespan import LifespanManager

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import JobStatus, JobType, ProposalStatus, ThesisAction
from stockbrain.jobs.handlers import register_ingestion_handlers
from stockbrain.jobs.registry import HandlerContext, JobRegistry
from stockbrain.main import create_app
from stockbrain.observability.health import ProviderHealthRegistry
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


class _Services:
    """The slice of the service container the proposal routes actually use."""

    def __init__(self, database: Database, **overrides: Any) -> None:
        self.proposals = ph.service(database, **overrides)
        self.account_state = self.proposals.account_state
        self.health = ProviderHealthRegistry()
        self.queue = self.proposals.queue
        self.research = None


async def _client(database: Database, **overrides: Any) -> tuple[httpx.AsyncClient, Any]:
    settings = ph.settings(**overrides)
    app = create_app(settings)
    app.state.database = database
    services = _Services(database, **overrides)
    manager = LifespanManager(app)
    await manager.__aenter__()
    app.state.services = services
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    return client, manager


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def test_the_policy_endpoint_reports_the_gate_and_the_absence_of_order_routes(
    clean_tables: Database,
) -> None:
    client, manager = await _client(clean_tables)
    try:
        response = await client.get("/api/v1/proposals/policy")
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)

    assert response.status_code == 200
    body = response.json()
    assert body["execution_policy"] == "MANUAL"
    assert body["automatic_authorization_permitted"] is False
    assert body["automation_blockers"]
    assert body["broker_automation"]["broker"] == "TRADING212"
    assert body["risk_policy_version"]
    assert body["risk_config"]["max_spread_bps"] == "50"
    assert body["broker_order_routes"] == []
    assert "No broker order has been sent" in body["notice"]


async def test_a_generated_proposal_is_listed_with_every_decision_input(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        await ph.service(clean_tables).generate(ph.THESIS_ID)
        listing = (await client.get("/api/v1/proposals")).json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["broker_ticker"] == "AAPL_US_EQ"
        assert item["market_symbol"] == "AAPL"
        assert item["company_name"] == "Apple Inc."
        assert item["side"] == "BUY"
        assert item["quote_spread_bps"] == "5.0000"
        assert item["quote_spread_status"] == "OK"
        assert item["reference_price"] == "200.05000000"
        assert item["execution_policy"] == "MANUAL"
        assert item["authorization_source"] is None
        assert item["can_approve"] and item["can_reject"] and item["can_cancel"]
        assert item["broker_order_transmitted"] is False
        assert item["risk_rules"]
        assert item["thesis_summary"] == "A thesis."

        detail = (await client.get(f"/api/v1/proposals/{item['id']}")).json()
        assert detail["id"] == item["id"]

        risk = (await client.get(f"/api/v1/proposals/{item['id']}/risk")).json()
        assert risk["risk_policy_version"] == item["risk_policy_version"]
        assert {rule["rule_id"] for rule in risk["rules"]} >= {
            "spread_ceiling",
            "quote_freshness",
            "instrument_identity",
            "min_cash_reserve",
        }
        assert risk["evaluations"][0]["stage"] == "GENERATION"
        assert risk["sizing_reasons"]
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_a_blocked_evaluation_is_visible_without_a_proposal(
    clean_tables: Database,
) -> None:
    """A refusal an operator cannot look at is indistinguishable from a bug."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        service = ph.service(
            clean_tables,
            market_data=ph.StubMarketData(bid=Decimal("305.33"), ask=Decimal("338.27")),
        )
        await service.generate(ph.THESIS_ID)
        rows = (await client.get("/api/v1/risk/evaluations?outcome=BLOCK")).json()
        assert len(rows) == 1
        assert rows[0]["proposal_id"] is None
        assert any(rule["rule_id"] == "spread_ceiling" for rule in rows[0]["rules"])
        assert (await client.get("/api/v1/proposals")).json()["total"] == 0
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_a_missing_proposal_is_a_404(clean_tables: Database) -> None:
    client, manager = await _client(clean_tables)
    try:
        response = await client.get(f"/api/v1/proposals/{uuid.uuid4()}")
        assert response.status_code == 404
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
async def test_approving_over_http_records_web_provenance(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        response = await client.post(f"/api/v1/proposals/{result.proposal_id}/approve", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "APPROVED"
        assert body["authorization_source"] == "HUMAN_WEB"
        assert body["approved_by"] == "web:local-operator"
        assert body["broker_order_transmitted"] is False
        assert body["can_approve"] is False
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_approving_twice_over_http_is_a_409(clean_tables: Database) -> None:
    """The double-click, at the HTTP boundary."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        path = f"/api/v1/proposals/{result.proposal_id}/approve"
        assert (await client.post(path, json={})).status_code == 200
        assert (await client.post(path, json={})).status_code == 409
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_approving_an_expired_proposal_is_a_410(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.update(TradeProposal).values(expires_at=utcnow() - dt.timedelta(seconds=1))
            )
        response = await client.post(f"/api/v1/proposals/{result.proposal_id}/approve", json={})
        assert response.status_code == 410
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_a_risk_refusal_at_approval_is_a_422_naming_the_rules(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    client, manager = await _client(clean_tables)
    try:
        service = ph.service(clean_tables, market_data=provider)
        result = await service.generate(ph.THESIS_ID)
        # Rebuild the app's service around the same provider so the widened
        # book is what the approval sees.
        app_services = _Services(clean_tables)
        app_services.proposals = service
        client._transport.app.state.services = app_services  # type: ignore[attr-defined]

        provider.bid, provider.ask = Decimal("180.00"), Decimal("220.00")
        response = await client.post(f"/api/v1/proposals/{result.proposal_id}/approve", json={})
        assert response.status_code == 422
        assert "risk" in response.json()["detail"].lower()
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_rejecting_over_http_is_durable(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        body = (
            await client.post(
                f"/api/v1/proposals/{result.proposal_id}/reject",
                json={"reason": "thesis is thin"},
            )
        ).json()
        assert body["status"] == "REJECTED"
        assert body["status_reason"] == "thesis is thin"
        assert body["rejected_by"] == "web:local-operator"
        assert (
            await client.post(f"/api/v1/proposals/{result.proposal_id}/approve", json={})
        ).status_code == 409
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_cancelling_over_http_works(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        body = (await client.post(f"/api/v1/proposals/{result.proposal_id}/cancel", json={})).json()
        assert body["status"] == "CANCELLED"
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_a_client_may_not_supply_any_order_parameter(clean_tables: Database) -> None:
    """The request body carries a reason and nothing else.

    A client that could name a quantity would be a client that could size a
    trade, which is the one thing the deterministic engine exists to own.
    """
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    client, manager = await _client(clean_tables)
    try:
        result = await ph.service(clean_tables).generate(ph.THESIS_ID)
        original = (await client.get(f"/api/v1/proposals/{result.proposal_id}")).json()
        for payload in (
            {"quantity": 9999},
            {"proposed_quantity": "9999"},
            {"broker_ticker": "TSLA_US_EQ"},
            {"reference_price": "1.00"},
            {"side": "SELL"},
            {"account_id": "someone-else"},
            {"reason": "ok", "quantity": 5},
        ):
            response = await client.post(
                f"/api/v1/proposals/{result.proposal_id}/approve", json=payload
            )
            assert response.status_code == 422, payload

        after = (await client.get(f"/api/v1/proposals/{result.proposal_id}")).json()
        assert after["proposed_quantity"] == original["proposed_quantity"]
        assert after["broker_ticker"] == original["broker_ticker"]
        assert after["side"] == original["side"]
        assert after["status"] == "READY", "no rejected payload changed anything"
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_the_api_reports_503_when_the_proposal_service_is_absent(
    clean_tables: Database,
) -> None:
    app = create_app(ph.settings())
    app.state.database = clean_tables
    async with LifespanManager(app):
        app.state.services = None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(f"/api/v1/proposals/{uuid.uuid4()}/approve", json={})
            assert response.status_code == 503


# ---------------------------------------------------------------------------
# Job wiring
# ---------------------------------------------------------------------------
async def test_the_generate_proposal_job_runs_the_service(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    registry = JobRegistry()
    register_ingestion_handlers(
        registry, classifier_available=False, proposals_available=True, account_sync_available=True
    )
    handler = registry.get(JobType.GENERATE_PROPOSAL.value)
    assert handler is not None

    services = _Services(clean_tables)
    await handler(
        HandlerContext(
            job_id=uuid.uuid4(),
            job_type=JobType.GENERATE_PROPOSAL.value,
            payload={"thesis_id": str(ph.THESIS_ID)},
            attempt=1,
            max_attempts=1,
            database=clean_tables,
            services=services,
        )
    )
    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TradeProposal))
        ).scalar_one() == 1


async def test_a_risk_block_is_an_answer_not_a_job_failure(clean_tables: Database) -> None:
    """Otherwise every blocked thesis would burn its whole retry budget."""
    await ph.seed(clean_tables, action=ThesisAction.HOLD)
    await ph.fund(clean_tables)
    registry = JobRegistry()
    register_ingestion_handlers(registry, classifier_available=False, proposals_available=True)
    handler = registry.get(JobType.GENERATE_PROPOSAL.value)
    assert handler is not None

    await handler(
        HandlerContext(
            job_id=uuid.uuid4(),
            job_type=JobType.GENERATE_PROPOSAL.value,
            payload={"thesis_id": str(ph.THESIS_ID)},
            attempt=1,
            max_attempts=1,
            database=clean_tables,
            services=_Services(clean_tables),
        )
    )  # must not raise


async def test_the_backlog_enqueues_one_job_per_published_thesis(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)

    assert await service.enqueue_pending() == 1
    assert await service.enqueue_pending() == 0, "the dedupe key suppresses the second"

    async with clean_tables.session() as session:
        jobs = list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.GENERATE_PROPOSAL.value)
                )
            ).scalars()
        )
    assert len(jobs) == 1
    assert jobs[0].payload == {"thesis_id": str(ph.THESIS_ID)}
    assert jobs[0].dedupe_key == f"proposal:{ph.THESIS_ID}"
    assert jobs[0].status is JobStatus.PENDING


async def test_the_backlog_skips_a_thesis_that_already_has_a_proposal(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)
    assert await service.enqueue_pending() == 0


async def test_the_backlog_skips_a_thesis_already_refused_under_this_policy(
    clean_tables: Database,
) -> None:
    """A blocked thesis must not be re-evaluated on every scheduler tick."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables, market_data=ph.StubMarketData(bid=Decimal("305.33"), ask=Decimal("338.27"))
    )
    await service.generate(ph.THESIS_ID)
    assert await service.enqueue_pending() == 0


async def test_a_completed_research_run_enqueues_its_proposal(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    assert await ph.service(clean_tables).enqueue_for_run(ph.RUN_ID) == 1
    async with clean_tables.session() as session:
        job = (
            await session.execute(
                sa.select(Job).where(Job.job_type == JobType.GENERATE_PROPOSAL.value)
            )
        ).scalar_one()
    assert job.payload["thesis_id"] == str(ph.THESIS_ID)


async def test_the_sweep_is_idempotent(clean_tables: Database) -> None:
    """A restart mid-sweep repeats work rather than losing it."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(BrokerInstrument).values(is_active=False))

    assert (await service.sweep())["invalidated"] == 1
    assert (await service.sweep())["invalidated"] == 0
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(TradeProposal.status))).scalar_one()
    assert status is ProposalStatus.INVALIDATED
