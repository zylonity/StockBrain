"""The whole pipeline, once, against a fake broker.

    event -> thesis -> resolved listing -> quote + account + FX
          -> deterministic risk -> proposal -> authorization
          -> execution -> reconciliation

Everything upstream of the risk engine is a controlled fixture, deliberately:
the classifier and the research graph are paid LLM calls with their own tests,
and spending model budget to re-prove they work would make this the slowest and
least reliable test in the suite. What is *not* faked is the part that matters
here — the engine, the sizing, the proposal row, the authorization provenance,
the send transaction, the ledger and the reconciler are all the real ones.

The single most important assertion in this file is ``provider.submitted == 1``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, RiskEvaluation, TradeProposal
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    ExecutionOutcome,
    OrderSide,
    ProposalStatus,
    ReconciliationResult,
    RiskOutcome,
)
from stockbrain.execution.reconciliation import ReconciliationService
from tests import proposal_helpers as ph
from tests.execution_helpers import (
    acknowledgement_for,
    build,
    execution_settings,
    order_view,
)
from tests.integration.test_cross_currency_proposals import (
    StubFxProvider,
    cross_currency_settings,
)

pytestmark = pytest.mark.integration

WEB_ACTOR = "web:owner"


async def _rows(database: Database, model: type[Any]) -> list[Any]:
    async with database.session() as session:
        return list((await session.execute(sa.select(model))).scalars())


async def _proposal(database: Database, proposal_id: uuid.UUID) -> TradeProposal:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal


async def _audit_actions(database: Database) -> list[str]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(AuditLog.action).order_by(AuditLog.occurred_at))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# The same-currency path
# ---------------------------------------------------------------------------
async def test_one_proposal_travels_the_whole_pipeline_and_sends_once(
    clean_tables: Database,
) -> None:
    """The acceptance run, with every stage asserted on its way past.

    Note what is checked at the end: exactly one broker POST, exactly one
    transmitted attempt, one mirrored broker order, and a proposal that reached
    ``EXECUTED`` through the real state machine.
    """
    settings = execution_settings()
    proposals, execution, provider = build(clean_tables, settings)

    # --- the controlled fixture: a published thesis on a resolved listing ---
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, environment=settings.t212_env.value)

    # --- generation: risk, sizing, the proposal row -------------------------
    generated = await proposals.generate(ph.THESIS_ID)
    assert generated.created, generated.reason
    assert generated.proposal_id is not None
    proposal_id = generated.proposal_id

    proposal = await _proposal(clean_tables, proposal_id)
    assert proposal.status is ProposalStatus.READY
    assert proposal.side is OrderSide.BUY
    assert proposal.proposed_quantity > 0
    assert proposal.risk_outcome in {RiskOutcome.ALLOW, RiskOutcome.REDUCE_SIZE}
    # The decision is recorded, hashed and attributable.
    assert proposal.risk_snapshot_hash
    assert proposal.risk_policy_version
    assert proposal.risk_rules

    evaluations = await _rows(clean_tables, RiskEvaluation)
    assert len(evaluations) == 1
    assert evaluations[0].stage == "GENERATION"

    # --- authorization: everything re-read and re-judged --------------------
    await proposals.authorize(proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=WEB_ACTOR)
    proposal = await _proposal(clean_tables, proposal_id)
    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.authorization_source is AuthorizationSource.HUMAN_WEB
    assert proposal.approved_by == WEB_ACTOR
    assert proposal.approved_at is not None
    # A second evaluation row, at the authorization stage.
    assert {row.stage for row in await _rows(clean_tables, RiskEvaluation)} == {
        "GENERATION",
        "AUTHORIZATION",
    }

    # --- execution: one POST ------------------------------------------------
    provider.acknowledgement = acknowledgement_for(
        order_view(
            # A numeric id: `acknowledgement_for` renders the payload the way
            # Trading 212 does, and the broker's `id` is documented int64.
            broker_order_id="500101",
            broker_ticker=proposal.broker_ticker,
            signed_quantity=proposal.proposed_quantity,
        )
    )
    result = await execution.execute(proposal_id)

    assert provider.submitted == 1, "exactly one broker POST"
    assert result.transmitted
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.broker_order_id == "500101"

    attempts = await _rows(clean_tables, ExecutionAttempt)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.sent_to_broker is True
    assert attempt.sent_at is not None
    assert attempt.ambiguous is False
    assert attempt.request_fingerprint
    # The immutable snapshot of everything the send was decided on.
    assert attempt.execution_snapshot
    assert attempt.execution_snapshot["broker_environment"] == "demo"

    # --- the broker's own record, mirrored ----------------------------------
    orders = await _rows(clean_tables, BrokerOrder)
    assert len(orders) == 1
    assert orders[0].broker_order_id == "500101"
    assert orders[0].broker_environment == "demo"

    # --- reconciliation: a fill, read rather than assumed -------------------
    provider.orders = {
        "500101": order_view(
            broker_order_id="500101",
            broker_ticker=proposal.broker_ticker,
            signed_quantity=proposal.proposed_quantity,
            filled_quantity=proposal.proposed_quantity,
            status="FILLED",
        )
    }
    reconciler = ReconciliationService(
        clean_tables, settings, provider=provider, proposals=proposals
    )
    outcome = await reconciler.reconcile(attempt.id)
    assert outcome.result is ReconciliationResult.ORDER_FOUND

    final = await _proposal(clean_tables, proposal_id)
    assert final.status is ProposalStatus.EXECUTED
    assert final.executed_at is not None

    # Still exactly one POST after reconciliation: the reconciler reads.
    assert provider.submitted == 1

    # --- the audit trail names every transition -----------------------------
    actions = await _audit_actions(clean_tables)
    for expected in (
        "proposal.generated",
        "proposal.authorized",
        "execution.transmitting",
    ):
        assert expected in actions, f"{expected} missing from {actions}"


# ---------------------------------------------------------------------------
# The cross-currency path -- the one the live account actually needs
# ---------------------------------------------------------------------------
async def test_a_gbp_account_sends_a_usd_order_once(clean_tables: Database) -> None:
    """The same journey on the account Phase 6 measured.

    GBP cash, a USD listing, a verified rate. Through Phase 8 this refused at
    ``currency_alignment`` before reaching the broker, which is why the Phase 8
    live order had to go through the adapter directly.
    """
    settings = cross_currency_settings(t212_execution_enabled=True)
    proposals, execution, provider = build(clean_tables, settings)
    fx_provider = StubFxProvider(rate=Decimal("1.35"))
    from stockbrain.fx.service import FxService

    proposals.fx = FxService(settings, provider=fx_provider)

    await ph.seed(clean_tables, currency="USD")
    await ph.fund(clean_tables, currency="GBP", environment=settings.t212_env.value)

    generated = await proposals.generate(ph.THESIS_ID)
    assert generated.created, generated.reason
    assert generated.proposal_id is not None

    proposal = await _proposal(clean_tables, generated.proposal_id)
    # The conversion is on the row, in full.
    assert proposal.fx_required is True
    assert proposal.fx_rate == Decimal("1.350000000000")
    assert proposal.fx_direction == "DIRECT"
    assert proposal.reference_currency == "USD"
    assert proposal.account_currency == "GBP"
    assert proposal.estimated_notional_account_currency is not None
    # And the account-currency cost is inside the account-currency cap, which is
    # the property the whole conversion exists to preserve.
    assert proposal.estimated_notional_account_currency <= Decimal("500")

    await proposals.authorize(
        generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=WEB_ACTOR
    )
    provider.acknowledgement = acknowledgement_for(
        order_view(
            broker_order_id="500102",
            broker_ticker=proposal.broker_ticker,
            signed_quantity=proposal.proposed_quantity,
        )
    )
    result = await execution.execute(generated.proposal_id)

    assert provider.submitted == 1
    assert result.outcome is ExecutionOutcome.SUBMITTED
    # The quantity on the wire is the quantity that was authorized, in shares,
    # never a converted number.
    assert provider.commands[0].quantity == proposal.proposed_quantity
    assert provider.commands[0].broker_ticker == proposal.broker_ticker

    # FX was resolved at generation, at authorization and once more immediately
    # before the send. Three independent reads, not one cached number.
    assert fx_provider.calls >= 3


# ---------------------------------------------------------------------------
# The negative acceptance criteria
# ---------------------------------------------------------------------------
async def test_the_pipeline_sends_nothing_when_the_master_switch_is_off(
    clean_tables: Database,
) -> None:
    """A fresh deployment runs the whole pipeline and transmits nothing.

    ``T212_EXECUTION_ENABLED`` defaults to false, so shipping execution does not
    by itself start sending orders.
    """
    settings = ph.settings(t212_execution_enabled=False)
    proposals, execution, provider = build(clean_tables, settings)
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, environment=settings.t212_env.value)

    generated = await proposals.generate(ph.THESIS_ID)
    assert generated.created
    assert generated.proposal_id is not None
    await proposals.authorize(
        generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=WEB_ACTOR
    )
    result = await execution.execute(generated.proposal_id)

    assert provider.submitted == 0
    assert not result.transmitted
    assert result.reason
    assert "T212_EXECUTION_ENABLED" in result.reason
    # The authorization survives: turning the switch on later must not require
    # re-deriving the trade.
    assert (await _proposal(clean_tables, generated.proposal_id)).status is ProposalStatus.APPROVED


async def test_an_unauthorized_proposal_is_never_transmitted(
    clean_tables: Database,
) -> None:
    """``READY`` is not ``APPROVED``.

    The single most important negative in the system: risk allowing a trade is
    not the same as somebody permitting it.
    """
    settings = execution_settings()
    proposals, execution, provider = build(clean_tables, settings)
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, environment=settings.t212_env.value)

    generated = await proposals.generate(ph.THESIS_ID)
    assert generated.created
    assert generated.proposal_id is not None

    result = await execution.execute(generated.proposal_id)
    assert provider.submitted == 0
    assert not result.transmitted
    assert await _rows(clean_tables, ExecutionAttempt) == []


async def test_the_kill_switch_stops_the_pipeline_at_authorization(
    clean_tables: Database,
) -> None:
    """And it is durable, so a restart comes back halted."""
    from stockbrain.control.state import ControlStateService
    from stockbrain.errors import AuthorizationNotPermitted

    settings = execution_settings()
    proposals, _execution, provider = build(clean_tables, settings)
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, environment=settings.t212_env.value)
    generated = await proposals.generate(ph.THESIS_ID)
    assert generated.proposal_id is not None

    await ControlStateService(clean_tables).engage_kill_switch(
        actor="test", source="test", reason="end-to-end drill"
    )
    with pytest.raises(AuthorizationNotPermitted):
        await proposals.authorize(
            generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=WEB_ACTOR
        )
    assert provider.submitted == 0

    # Released, and the same proposal proceeds -- a halt does not destroy work.
    await ControlStateService(clean_tables).release_kill_switch(
        actor="test", source="test", reason="drill over"
    )
    await proposals.authorize(
        generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=WEB_ACTOR
    )
    assert (await _proposal(clean_tables, generated.proposal_id)).status is ProposalStatus.APPROVED
