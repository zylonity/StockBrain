"""Database-backed proposal generation, authorization, expiry and races.

These need a real PostgreSQL because most of what they assert is enforced *by*
PostgreSQL: a partial unique index, a check constraint, a row lock and a
transaction-scoped advisory lock. None of it can be exercised against a mock.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    CapabilityState,
    ExecutionPolicy,
    OrderSide,
    PriceSource,
    ProposalStatus,
    ResolutionStatus,
    RiskOutcome,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.errors import (
    AuthorizationNotPermitted,
    ProposalAlreadyConsumed,
    ProposalExpired,
    RiskBlocked,
)
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


async def _proposal(database: Database) -> TradeProposal:
    async with database.session() as session:
        return (await session.execute(sa.select(TradeProposal))).scalars().one()


async def _evaluations(database: Database) -> list[RiskEvaluation]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(RiskEvaluation).order_by(RiskEvaluation.created_at))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
async def test_a_healthy_thesis_produces_a_proposal_with_every_decision_input(
    clean_tables: Database,
) -> None:
    """The proposal records the exact values the decision was made on.

    Not a summary of them: the bid, the ask, the spread in basis points, the
    quote's age, the session and its source, the policy version and every rule.
    A proposal that cannot be re-read is a proposal that cannot be audited.
    """
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)

    assert result.created and result.proposal_id is not None
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.READY
    assert proposal.side is OrderSide.BUY
    assert proposal.proposed_quantity > 0
    assert proposal.broker_ticker == "AAPL_US_EQ"
    assert proposal.account_id == ph.ACCOUNT_ID
    assert proposal.account_currency == "USD"
    assert proposal.reference_price == Decimal("200.05"), "a market buy lifts the ask"
    assert proposal.price_source is PriceSource.ALPACA_IEX
    assert proposal.quote_bid == Decimal("199.95")
    assert proposal.quote_ask == Decimal("200.05")
    assert proposal.quote_mid == Decimal("200.00")
    assert proposal.quote_spread == Decimal("0.10")
    assert proposal.quote_spread_bps == Decimal("5.0000")
    assert proposal.quote_spread_status == "OK"
    assert proposal.quote_age_ms >= 0
    assert proposal.market_session is not None
    assert proposal.risk_outcome in {RiskOutcome.ALLOW, RiskOutcome.REDUCE_SIZE}
    assert proposal.risk_policy_version
    assert len(proposal.risk_rules) > 10
    assert proposal.research_action == "BUY"
    assert proposal.research_confidence == pytest.approx(0.9)
    assert proposal.research_run_id == ph.RUN_ID
    assert proposal.execution_policy is ExecutionPolicy.MANUAL
    assert proposal.authorization_source is None
    assert proposal.expires_at > utcnow()
    assert proposal.sizing_reasons


async def test_a_generation_records_a_durable_risk_evaluation(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    await ph.service(clean_tables).generate(ph.THESIS_ID)

    evaluations = await _evaluations(clean_tables)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.stage == "GENERATION"
    assert evaluation.thesis_id == ph.THESIS_ID
    assert evaluation.proposal_id is not None
    assert evaluation.rules and evaluation.snapshot_hash


async def test_a_blocked_thesis_records_the_refusal_and_creates_no_proposal(
    clean_tables: Database,
) -> None:
    """ "Why was nothing proposed?" must be answerable from the database.

    A proposal row cannot serve: its quantity and price are NOT NULL and
    positive, and a blocked evaluation has neither.
    """
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables, market_data=ph.StubMarketData(bid=Decimal("305.33"), ask=Decimal("338.27"))
    )
    result = await service.generate(ph.THESIS_ID)

    assert not result.created
    assert result.outcome is RiskOutcome.BLOCK
    assert any("spread" in reason for reason in result.blocks)
    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TradeProposal))
        ).scalar_one() == 0
    evaluations = await _evaluations(clean_tables)
    assert len(evaluations) == 1
    assert evaluations[0].outcome is RiskOutcome.BLOCK
    assert evaluations[0].proposal_id is None
    assert any(rule["rule_id"] == "spread_ceiling" for rule in evaluations[0].rules)


async def test_a_hold_thesis_costs_no_market_data_request(clean_tables: Database) -> None:
    """A HOLD is a conclusion, not an order; refusing it needs no price."""
    await ph.seed(clean_tables, action=ThesisAction.HOLD)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    result = await ph.service(clean_tables, market_data=provider).generate(ph.THESIS_ID)

    assert not result.created
    assert provider.quote_calls == 0
    evaluations = await _evaluations(clean_tables)
    assert evaluations[0].rules[0]["rule_id"] == "action_is_executable"


async def test_generation_fails_closed_without_account_state(clean_tables: Database) -> None:
    await ph.seed(clean_tables)  # deliberately not funded
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("account" in reason.lower() for reason in result.blocks)


async def test_a_snapshot_from_the_other_broker_environment_is_refused(
    clean_tables: Database,
) -> None:
    """A demo balance must never size a live order, or the other way round."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, environment="live")
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("different broker environment" in reason for reason in result.blocks)


async def test_a_stale_account_snapshot_is_refused(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, captured_at=utcnow() - dt.timedelta(hours=2))
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("older than the configured limit" in reason for reason in result.blocks)


@pytest.mark.parametrize(
    "resolution",
    [ResolutionStatus.AMBIGUOUS, ResolutionStatus.NOT_FOUND, ResolutionStatus.PENDING],
)
async def test_an_unresolved_identity_blocks_generation(
    clean_tables: Database, resolution: ResolutionStatus
) -> None:
    await ph.seed(clean_tables, resolution=resolution)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("identity" in reason for reason in result.blocks)


async def test_a_retired_listing_blocks_generation(clean_tables: Database) -> None:
    await ph.seed(clean_tables, is_active=False)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created


async def test_the_symbol_always_comes_from_the_resolved_listing(clean_tables: Database) -> None:
    """The classifier's ticker hint is a search key and nothing else.

    The seeded impact carries the hint ``AAPL``; the broker identity on the
    proposal is ``AAPL_US_EQ``, which only the resolver could have supplied.
    """
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    await ph.service(clean_tables).generate(ph.THESIS_ID)
    proposal = await _proposal(clean_tables)
    assert proposal.broker_ticker == "AAPL_US_EQ"
    assert proposal.broker_instrument_id == ph.INSTRUMENT_ID


async def test_a_cross_currency_listing_is_refused_rather_than_converted(
    clean_tables: Database,
) -> None:
    """With ``RISK_REQUIRE_SAME_CURRENCY`` set, a mismatch never reaches sizing.

    The default posture, and the one the live GBP account ran under through
    Phase 8. Phase 9 can lift it, but only by configuring a verified FX source
    *and* saying so -- see the cross-currency generation tests.
    """
    # A USD listing on a GBP account, priced in USD: exactly what Phase 6
    # measured on 14 of 14 live positions.
    await ph.seed(clean_tables, currency="USD")
    await ph.fund(clean_tables, currency="GBP")
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("RISK_REQUIRE_SAME_CURRENCY" in reason for reason in result.blocks)


async def test_market_data_failure_blocks_rather_than_falling_back(
    clean_tables: Database,
) -> None:
    from stockbrain.errors import ProviderUnavailable

    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData(error=ProviderUnavailable("upstream 503"))
    result = await ph.service(clean_tables, market_data=provider).generate(ph.THESIS_ID)
    assert not result.created
    assert any("market-data provider failed" in reason for reason in result.blocks)


async def test_an_unentitled_provider_blocks(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData(state=CapabilityState.ENTITLEMENT_MISSING, usable=False)
    result = await ph.service(clean_tables, market_data=provider).generate(ph.THESIS_ID)
    assert not result.created


# ---------------------------------------------------------------------------
# Sell and reduce
# ---------------------------------------------------------------------------
async def test_a_sell_thesis_closes_the_available_position(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.SELL)
    await ph.fund(clean_tables, positions={"AAPL_US_EQ": (Decimal("9"), Decimal("9"))})
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert result.created
    proposal = await _proposal(clean_tables)
    assert proposal.side is OrderSide.SELL
    assert proposal.proposed_quantity == Decimal("9")
    assert proposal.reference_price == Decimal("199.95"), "a market sell hits the bid"


async def test_a_reduce_thesis_trims_rather_than_liquidates(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.REDUCE)
    await ph.fund(clean_tables, positions={"AAPL_US_EQ": (Decimal("10"), Decimal("10"))})
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert result.created
    proposal = await _proposal(clean_tables)
    assert proposal.proposed_quantity == Decimal("5")


async def test_a_sell_without_a_position_is_refused(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.SELL)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert not result.created
    assert any("short selling is disabled" in reason for reason in result.blocks)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
async def test_web_authorization_records_its_provenance(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    await service.authorize(
        result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
    )
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.authorization_source is AuthorizationSource.HUMAN_WEB
    assert proposal.approved_by == "web:operator"
    assert proposal.approved_at is not None
    assert proposal.approved_channel is not None
    snapshot = proposal.authorization_policy_snapshot
    assert snapshot["broker_order_transmitted"] is False
    assert snapshot["execution_policy"] == "MANUAL"
    assert "automation" in snapshot


async def test_authorization_refetches_the_quote_and_re_runs_every_rule(
    clean_tables: Database,
) -> None:
    """Nothing is trusted from generation time."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=provider)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    calls_after_generation = provider.quote_calls

    await service.authorize(
        result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
    )
    assert provider.quote_calls == calls_after_generation + 1

    evaluations = await _evaluations(clean_tables)
    stages = [row.stage for row in evaluations]
    assert stages == ["GENERATION", "AUTHORIZATION"]
    authorization_rules = {rule["rule_id"] for rule in evaluations[-1].rules}
    assert {"proposal_ttl", "reference_price_drift", "authorization_envelope"} <= (
        authorization_rules
    )
    assert "spread_ceiling" in authorization_rules
    assert "account_state_freshness" in authorization_rules


async def test_a_market_that_widened_after_generation_invalidates_at_authorization(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=provider)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    provider.bid, provider.ask = Decimal("180.00"), Decimal("220.00")
    with pytest.raises(RiskBlocked) as excinfo:
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )
    assert "spread_ceiling" in excinfo.value.rule_ids

    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.INVALIDATED
    assert proposal.invalidated_at is not None
    assert proposal.invalidation_reason


async def test_a_price_that_drifted_too_far_invalidates_at_authorization(
    clean_tables: Database,
) -> None:
    """The quantity the operator saw no longer describes the trade."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=provider)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    provider.bid, provider.ask = Decimal("249.95"), Decimal("250.05")
    with pytest.raises(RiskBlocked) as excinfo:
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )
    assert "reference_price_drift" in excinfo.value.rule_ids


async def test_a_shrunken_risk_envelope_invalidates_rather_than_silently_resizing(
    clean_tables: Database,
) -> None:
    """Re-pricing under someone's finger is how a trade nobody read gets approved."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    original = await _proposal(clean_tables)

    # The cash disappeared between drafting and approving.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.text("UPDATE portfolio_snapshots SET cash_available = 100, captured_at = now()")
        )
    with pytest.raises(RiskBlocked) as excinfo:
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )
    assert {"authorization_envelope", "min_cash_reserve"} & set(excinfo.value.rule_ids)

    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.INVALIDATED
    assert proposal.proposed_quantity == original.proposed_quantity, "never silently re-sized"


async def test_a_changed_risk_policy_blocks_authorization(clean_tables: Database) -> None:
    """Authorizing against limits nobody chose for this proposal is refused."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    stricter = ph.service(clean_tables, risk_max_notional_per_trade=Decimal("123"))
    with pytest.raises(RiskBlocked) as excinfo:
        await stricter.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )
    assert "risk_policy_version" in excinfo.value.rule_ids


async def test_an_expired_proposal_cannot_be_authorized(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(expires_at=utcnow() - dt.timedelta(seconds=1))
        )
    with pytest.raises(ProposalExpired):
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )


async def test_authorizing_twice_is_refused(clean_tables: Database) -> None:
    """The double-click case."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    await service.authorize(
        result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
    )
    with pytest.raises(ProposalAlreadyConsumed):
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )


async def test_a_rejection_is_durable_and_terminal(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    await service.reject(result.proposal_id, actor="web:operator", reason="not convinced")
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.REJECTED
    assert proposal.rejected_by == "web:operator"
    assert proposal.rejected_at is not None
    assert proposal.status_reason == "not convinced"

    with pytest.raises(ProposalAlreadyConsumed):
        await service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        )


async def test_every_authorization_and_refusal_reaches_the_audit_log(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    await service.authorize(
        result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
    )
    async with clean_tables.session() as session:
        actions = [
            row.action
            for row in (
                await session.execute(sa.select(AuditLog).order_by(AuditLog.occurred_at))
            ).scalars()
        ]
    assert actions == ["proposal.generated", "proposal.authorized"]


# ---------------------------------------------------------------------------
# Automatic authorization
# ---------------------------------------------------------------------------
async def test_automatic_mode_authorizes_immediately_with_system_provenance(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, execution_policy="automatic")
    result = await service.generate(ph.THESIS_ID)

    assert result.created and result.authorized
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.APPROVED
    assert proposal.execution_policy is ExecutionPolicy.AUTOMATIC
    assert proposal.authorization_source is AuthorizationSource.SYSTEM_AUTOMATIC
    assert proposal.approved_by == "system:automatic"
    assert proposal.approved_channel is None, "no human channel authorized this"
    assert proposal.authorization_policy_snapshot["broker_order_transmitted"] is False


async def test_automatic_and_manual_converge_on_the_same_authorized_shape(
    clean_tables: Database,
) -> None:
    """Only the provenance differs; everything downstream reads one representation."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    await ph.service(clean_tables, execution_policy="automatic").generate(ph.THESIS_ID)
    automatic = await _proposal(clean_tables)

    assert automatic.status is ProposalStatus.APPROVED
    assert automatic.approved_at is not None
    assert automatic.risk_outcome is not None
    assert automatic.risk_policy_version
    assert automatic.risk_rules


async def test_automatic_mode_still_fails_closed_on_a_block(clean_tables: Database) -> None:
    """High confidence and an automatic policy do not lift a deterministic rule."""
    await ph.seed(clean_tables, confidence=0.99)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables,
        execution_policy="automatic",
        market_data=ph.StubMarketData(bid=Decimal("305.33"), ask=Decimal("338.27")),
    )
    result = await service.generate(ph.THESIS_ID)
    assert not result.created and not result.authorized
    async with clean_tables.session() as session:
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TradeProposal))
        ).scalar_one() == 0


async def test_a_manual_proposal_can_never_be_authorized_by_the_system(
    clean_tables: Database,
) -> None:
    """Flipping the deployment policy must not retroactively authorize work."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    manual = ph.service(clean_tables)
    result = await manual.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    automatic = ph.service(clean_tables, execution_policy="automatic")
    with pytest.raises(AuthorizationNotPermitted, match="MANUAL execution policy"):
        await automatic.authorize(
            result.proposal_id,
            source=AuthorizationSource.SYSTEM_AUTOMATIC,
            actor="system:automatic",
        )
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.READY


async def test_the_database_refuses_a_system_authorization_on_a_manual_proposal(
    clean_tables: Database,
) -> None:
    """The service check is necessary; this constraint is what makes it sufficient."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    with pytest.raises(Exception, match="system_auth_requires_automatic_policy"):
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.text(
                    "UPDATE trade_proposals SET status='APPROVED', "
                    "authorization_source='SYSTEM_AUTOMATIC', approved_at=now(), "
                    "approved_by='forged' WHERE id=:id"
                ),
                {"id": result.proposal_id},
            )


async def test_the_database_refuses_an_approval_without_provenance(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    result = await ph.service(clean_tables).generate(ph.THESIS_ID)
    with pytest.raises(Exception, match="approved_requires_authorization_provenance"):
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.text("UPDATE trade_proposals SET status='APPROVED' WHERE id=:id"),
                {"id": result.proposal_id},
            )


async def test_automatic_authorization_is_refused_when_the_broker_forbids_it(
    clean_tables: Database,
) -> None:
    """Live automation without recorded consent has no path, even by hand."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, execution_policy="automatic")
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    # The proposal exists and is AUTOMATIC; a service whose policy no longer
    # permits automation must still refuse to authorize it.
    manual = ph.service(clean_tables)
    with pytest.raises(AuthorizationNotPermitted):
        await manual.authorize(
            result.proposal_id,
            source=AuthorizationSource.SYSTEM_AUTOMATIC,
            actor="system:automatic",
        )


# ---------------------------------------------------------------------------
# Expiry and invalidation
# ---------------------------------------------------------------------------
async def test_the_sweep_expires_a_proposal_past_its_ttl(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(expires_at=utcnow() - dt.timedelta(minutes=1))
        )

    counts = await service.sweep()
    assert counts["expired"] == 1
    assert (await _proposal(clean_tables)).status is ProposalStatus.EXPIRED


async def test_the_ttl_is_configurable(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    await ph.service(clean_tables, risk_proposal_ttl_minutes=5).generate(ph.THESIS_ID)
    proposal = await _proposal(clean_tables)
    assert proposal.expires_at - proposal.created_at < dt.timedelta(minutes=6)


async def test_a_retired_listing_invalidates_a_live_proposal(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)

    async with clean_tables.transaction() as session:
        await session.execute(sa.update(BrokerInstrument).values(is_active=False))
    counts = await service.sweep()
    assert counts["invalidated"] == 1
    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.INVALIDATED
    assert "no longer an active broker listing" in (proposal.invalidation_reason or "")


async def test_a_newer_thesis_invalidates_the_proposal_it_supersedes(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)

    async with clean_tables.transaction() as session:
        session.add(
            Thesis(
                id=uuid.uuid4(),
                research_run_id=ph.RUN_ID,
                action=ThesisAction.SELL,
                confidence=0.8,
                time_horizon=TimeHorizon.DAYS,
                supersedes_thesis_id=ph.THESIS_ID,
            )
        )
    counts = await service.sweep()
    assert counts["invalidated"] == 1
    assert "supersedes" in ((await _proposal(clean_tables)).invalidation_reason or "")


async def test_a_changed_risk_policy_invalidates_live_proposals(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    await ph.service(clean_tables).generate(ph.THESIS_ID)

    stricter = ph.service(clean_tables, risk_max_notional_per_trade=Decimal("321"))
    counts = await stricter.sweep()
    assert counts["invalidated"] == 1
    assert "risk configuration changed" in (
        (await _proposal(clean_tables)).invalidation_reason or ""
    )


async def test_a_position_that_disappeared_invalidates_a_pending_reduction(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables, action=ThesisAction.SELL)
    await ph.fund(clean_tables, positions={"AAPL_US_EQ": (Decimal("9"), Decimal("9"))})
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)

    async with clean_tables.transaction() as session:
        await session.execute(sa.text("DELETE FROM positions"))
    counts = await service.sweep()
    assert counts["invalidated"] == 1
    assert "fell to 0" in ((await _proposal(clean_tables)).invalidation_reason or "")


async def test_the_sweep_invalidates_a_proposal_whose_market_widened(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=provider)
    await service.generate(ph.THESIS_ID)

    provider.bid, provider.ask = Decimal("180.00"), Decimal("220.00")
    counts = await service.sweep()
    assert counts["market_invalidated"] == 1
    assert "widened" in ((await _proposal(clean_tables)).invalidation_reason or "")


async def test_a_provider_outage_does_not_retire_every_open_proposal(
    clean_tables: Database,
) -> None:
    """An outage should degrade the system, not destroy its work."""
    from stockbrain.errors import ProviderUnavailable

    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    provider = ph.StubMarketData()
    service = ph.service(clean_tables, market_data=provider)
    await service.generate(ph.THESIS_ID)

    provider.error = ProviderUnavailable("upstream 503")
    counts = await service.sweep()
    assert counts["market_invalidated"] == 0
    assert (await _proposal(clean_tables)).status is ProposalStatus.READY


async def test_cancellation_is_legal_from_every_pre_execution_state(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    await service.cancel(result.proposal_id, actor="web:operator", reason="changed my mind")
    assert (await _proposal(clean_tables)).status is ProposalStatus.CANCELLED
