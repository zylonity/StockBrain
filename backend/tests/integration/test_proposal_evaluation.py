"""Exercise the shared evaluator through each public proposal entry point."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import PortfolioSnapshot
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.db.session import Database
from stockbrain.enums import AuthorizationSource, ProposalStatus
from stockbrain.errors import ProviderUnavailable, RiskBlocked
from stockbrain.fx.service import FxService
from tests import proposal_helpers as ph
from tests.integration.test_cross_currency_proposals import StubFxProvider, cross_currency_settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "source", [AuthorizationSource.HUMAN_WEB, AuthorizationSource.HUMAN_TELEGRAM]
)
async def test_generation_authorization_and_revalidation_agree_on_healthy_risk(
    clean_tables: Database, source: AuthorizationSource
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    now = utcnow()
    generated = await service.generate(ph.THESIS_ID, now=now)
    assert generated.created and generated.proposal_id is not None
    revalidated = await service.revalidate(generated.proposal_id, now=now)
    authorized = await service.authorize(
        generated.proposal_id, source=source, actor="test:owner", now=now
    )
    assert revalidated is not None and revalidated.allowed
    assert authorized.decision is not None
    async with clean_tables.session() as session:
        generation = await session.get(RiskEvaluation, generated.evaluation_id)
        authorization = await session.get(RiskEvaluation, authorized.evaluation_id)
        assert generation is not None and authorization is not None
        assert generation.outcome == revalidated.decision.outcome == authorized.decision.outcome
        assert generation.snapshot["sizing"] == revalidated.decision.sizing.as_dict()
        assert revalidated.decision.sizing == authorized.decision.sizing
        core_rules = [(rule["rule_id"], rule["outcome"]) for rule in generation.rules]
        assert core_rules == [
            (rule.rule_id, rule.outcome.value) for rule in revalidated.decision.rules
        ]
        assert authorization.rules == [rule.as_dict() for rule in revalidated.rules]
        # Each existing-proposal path must exclude its own cash reservation.
        assert revalidated.decision.inputs_summary["reserved"]["active_proposals"] == 0
        assert authorized.decision.inputs_summary["reserved"]["active_proposals"] == 0


@pytest.mark.parametrize(
    "defect,rule_id,generation_rule",
    [
        ("stale_account", "account_state_available", True),
        ("stale_quote", "quote_freshness", True),
        ("spread", "spread_ceiling", True),
        ("missing_fx", "fx_available", True),
        ("fx_drift", "fx_rate_drift", False),
        ("price_drift", "reference_price_drift", False),
        ("confidence", "research_confidence_floor", True),
        ("policy", "risk_policy_version", False),
    ],
)
@pytest.mark.parametrize(
    "source", [AuthorizationSource.HUMAN_WEB, AuthorizationSource.SYSTEM_AUTOMATIC]
)
async def test_a_changed_world_has_the_same_refusal_at_authorization_and_revalidation(
    clean_tables: Database,
    defect: str,
    rule_id: str,
    generation_rule: bool,
    source: AuthorizationSource,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables, currency="GBP")
    settings = cross_currency_settings()
    market = ph.StubMarketData()
    fx = StubFxProvider()
    service = ph.service_with(clean_tables, settings, market_data=market)
    service.fx = FxService(settings, provider=fx)
    generated = await service.generate(ph.THESIS_ID)
    assert generated.created and generated.proposal_id is not None
    if source is AuthorizationSource.SYSTEM_AUTOMATIC:
        # Keep the initial generation unapproved so the automatic refusal can
        # be observed independently of the generation-time authorization call.
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.text("UPDATE trade_proposals SET execution_policy='AUTOMATIC'")
            )
        service.settings = settings.model_copy(
            update={"execution_policy": ph.settings(execution_policy="automatic").execution_policy}
        )

    if defect == "stale_account":
        async with clean_tables.transaction() as session:
            await session.execute(
                sa.update(PortfolioSnapshot).values(captured_at=utcnow() - dt.timedelta(hours=1))
            )
    elif defect == "stale_quote":
        market.age_ms = 60_000
    elif defect == "spread":
        market.bid, market.ask = Decimal("180"), Decimal("220")
    elif defect == "missing_fx":
        fx.error = ProviderUnavailable("FX feed unavailable")
    elif defect == "fx_drift":
        fx._rate = Decimal("1.45")
    elif defect == "price_drift":
        market.bid, market.ask = Decimal("249.95"), Decimal("250.05")
    elif defect == "confidence":
        async with clean_tables.transaction() as session:
            await session.execute(sa.update(Thesis).values(confidence=0.1))
            await session.execute(sa.update(TradeProposal).values(research_confidence=0.1))
    else:
        service.config = replace(service.config, max_notional_per_trade=Decimal("123"))

    now = utcnow()
    revalidated = await service.revalidate(generated.proposal_id, now=now)
    assert revalidated is not None and rule_id in revalidated.rule_ids
    with pytest.raises(RiskBlocked) as refusal:
        await service.authorize(generated.proposal_id, source=source, actor="test:owner", now=now)
    assert refusal.value.rule_ids == revalidated.rule_ids
    async with clean_tables.session() as session:
        row = await session.get(TradeProposal, generated.proposal_id)
        assert row is not None and row.status is ProposalStatus.INVALIDATED
        authorization = await session.scalar(
            sa.select(RiskEvaluation).where(RiskEvaluation.stage == "AUTHORIZATION")
        )
        assert authorization is not None
        assert [(r["rule_id"], r["outcome"]) for r in authorization.rules] == [
            (r.rule_id, r.outcome.value) for r in revalidated.rules
        ]

    if generation_rule:
        regenerated = await service.generate(ph.THESIS_ID, now=now)
        assert not regenerated.created and regenerated.evaluation_id is not None
        async with clean_tables.session() as session:
            evaluation = await session.get(RiskEvaluation, regenerated.evaluation_id)
            assert evaluation is not None
            assert any(
                rule["rule_id"] == rule_id and rule["outcome"] == "BLOCK"
                for rule in evaluation.rules
            )
