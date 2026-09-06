"""Account currency is the unit of every durable cash reservation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import ROUND_CEILING, Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import AuthorizationSource
from stockbrain.errors import ProviderUnavailable, RiskBlocked
from stockbrain.fx.service import FxService
from stockbrain.market_data.base import Quote
from stockbrain.proposals.service import ProposalService
from tests import proposal_helpers as ph
from tests.integration.test_cross_currency_proposals import StubFxProvider, cross_currency_settings
from tests.integration.test_proposal_concurrency import SECOND_THESIS, _seed_second_listing

pytestmark = pytest.mark.integration


class CurrencyMarket(ph.StubMarketData):
    def __init__(self, currency: str) -> None:
        super().__init__(bid=Decimal("99.98"), ask=Decimal("100"))
        self.currency = currency

    async def latest_quote(self, symbol: str) -> Quote:
        return replace(await super().latest_quote(symbol), currency=self.currency)


async def setup(
    database: Database,
    account: str,
    listing: str,
    *,
    cash: Decimal = Decimal("100000"),
    cap: Decimal = Decimal("500"),
) -> tuple[ProposalService, StubFxProvider, CurrencyMarket]:
    await ph.seed(database, currency=listing)
    await _seed_second_listing(database)
    async with database.transaction() as session:
        await session.execute(sa.update(BrokerInstrument).values(currency=listing))
    await ph.fund(database, currency=account, cash=cash, total=cash, invested=Decimal("0"))
    settings = cross_currency_settings(
        risk_max_notional_per_trade=cap,
        risk_max_position_pct=Decimal("1"),
        risk_max_aggregate_exposure_pct=Decimal("1"),
        risk_max_active_proposal_exposure_pct=Decimal("1"),
    )
    market = CurrencyMarket(listing)
    fx = StubFxProvider(rate=Decimal("1.25"))
    service = ph.service_with(database, settings, market_data=market)
    service.fx = FxService(settings, provider=fx)
    return service, fx, market


def converted(amount: Decimal, account: str, listing: str) -> Decimal:
    if account == listing:
        return amount
    return amount * Decimal("1.25") if account == "USD" else amount / Decimal("1.25")


@pytest.mark.parametrize("account,listing", [("USD", "USD"), ("USD", "GBP"), ("GBP", "USD")])
async def test_generation_and_revalidation_reserve_account_currency_and_release_it(
    clean_tables: Database,
    account: str,
    listing: str,
) -> None:
    service, fx, _ = await setup(clean_tables, account, listing)
    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(SECOND_THESIS)
    assert first.proposal_id is not None and second.proposal_id is not None
    async with clean_tables.session() as session:
        row = await session.get(TradeProposal, first.proposal_id)
        assert row is not None
        expected = converted(row.estimated_notional, account, listing)
        assert row.estimated_notional_account_currency == expected
        if (account, listing) == ("USD", "GBP"):
            assert row.estimated_notional == Decimal("400")
            assert expected == Decimal("500")
    verdict = await service.revalidate(second.proposal_id)
    assert verdict is not None and verdict.allowed
    assert Decimal(verdict.decision.inputs_summary["reserved"]["reserved_notional"]) == expected
    assert fx.calls == (0 if account == listing else 3), "no separate reservation FX reads"
    await service.reject(first.proposal_id, actor="test:owner")
    released = await service.revalidate(second.proposal_id)
    assert released is not None and released.allowed
    assert released.decision.inputs_summary["reserved"]["reserved_notional"] == "0"


@pytest.mark.parametrize("account,listing", [("USD", "USD"), ("USD", "GBP"), ("GBP", "USD")])
async def test_concurrent_generators_cannot_oversubscribe_one_account_currency_budget(
    clean_tables: Database,
    account: str,
    listing: str,
) -> None:
    service, fx, _ = await setup(
        clean_tables, account, listing, cash=Decimal("1000"), cap=Decimal("800")
    )
    results = await asyncio.gather(service.generate(ph.THESIS_ID), service.generate(SECOND_THESIS))
    assert all(result.created for result in results), "both the full and remaining cash are usable"
    async with clean_tables.session() as session:
        rows = list((await session.scalars(sa.select(TradeProposal))).all())
    actual = sum((converted(row.estimated_notional, account, listing) for row in rows), Decimal(0))
    assert actual <= Decimal("900"), "10% of the 1000 account must remain cash"
    assert (
        sum((row.estimated_notional_account_currency or Decimal(0) for row in rows), Decimal(0))
        == actual
    )
    assert fx.calls == (0 if account == listing else 2)


@pytest.mark.parametrize("account,listing", [("USD", "GBP"), ("GBP", "USD")])
async def test_authorization_refreshes_fixed_quantity_cost_and_fx_provenance(
    clean_tables: Database,
    account: str,
    listing: str,
) -> None:
    service, fx, market = await setup(clean_tables, account, listing)
    first = await service.generate(ph.THESIS_ID)
    assert first.proposal_id is not None
    # Increase the new envelope while retaining the original proposed quantity.
    # Cost must use that quantity, not the newly suggested size.
    async with clean_tables.transaction() as session:
        row = await session.get(TradeProposal, first.proposal_id)
        assert row is not None
        row.proposed_quantity = Decimal("1")
        row.estimated_notional = Decimal("100")
        row.estimated_notional_account_currency = converted(Decimal("100"), account, listing)
    fx._rate = Decimal("1.254")
    market.bid, market.ask = Decimal("100.08"), Decimal("100.10")
    authorized = await service.authorize(
        first.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="test:owner"
    )
    assert authorized.decision is not None and authorized.decision.sizing.quantity > Decimal("1")
    async with clean_tables.session() as session:
        row = await session.get(TradeProposal, first.proposal_id)
        assert row is not None
        assert row.proposed_quantity == Decimal("1")
        expected = (
            Decimal("100.10") * fx._rate if account == "USD" else Decimal("100.10") / fx._rate
        )
        assert row.estimated_notional_account_currency == expected.quantize(
            Decimal("0.0001"), rounding=ROUND_CEILING
        )
        assert row.fx_rate == fx._rate
        assert row.fx_provider == "stub_fx" and row.fx_rate_grade == "EXECUTION"
        assert row.fx_provider_timestamp is not None and row.fx_received_at is not None
        assert (
            row.fx_provider_timestamp.isoformat()
            == row.risk_snapshot["inputs"]["fx"]["provider_timestamp"]
        )
        assert row.fx_direction == ("DIRECT" if account == "GBP" else "INVERTED")
    assert fx.calls == 2
    assert (await service.generate(SECOND_THESIS)).created
    verdict = await service.revalidate(first.proposal_id)
    assert verdict is not None and verdict.allowed


@pytest.mark.parametrize("missing", ["rate", "converted_amount", "currency_mismatch"])
async def test_unknown_conversion_fails_closed_at_every_decision_stage(
    clean_tables: Database,
    missing: str,
) -> None:
    service, fx, _ = await setup(clean_tables, "USD", "GBP")
    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(SECOND_THESIS)
    assert first.proposal_id is not None and second.proposal_id is not None
    if missing == "rate":
        fx.error = ProviderUnavailable("FX unavailable")
        rule = "fx_available"
    else:
        async with clean_tables.transaction() as session:
            row = await session.get(TradeProposal, first.proposal_id)
            assert row is not None
            if missing == "converted_amount":
                row.estimated_notional_account_currency = None
            else:
                row.account_currency = "EUR"
        rule = "reservation_accounting"
    verdict = await service.revalidate(second.proposal_id)
    assert verdict is not None and rule in verdict.rule_ids
    with pytest.raises(RiskBlocked) as refusal:
        await service.authorize(
            second.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="test:owner"
        )
    assert rule in refusal.value.rule_ids
    regenerated = await service.generate(SECOND_THESIS)
    assert not regenerated.created and regenerated.evaluation_id is not None
    async with clean_tables.session() as session:
        evaluation = await session.get(RiskEvaluation, regenerated.evaluation_id)
        assert evaluation is not None
        assert any(r["rule_id"] == rule and r["outcome"] == "BLOCK" for r in evaluation.rules)


async def test_legacy_same_currency_reservations_need_no_fx_or_converted_column(
    clean_tables: Database,
) -> None:
    service, fx, _ = await setup(clean_tables, "USD", "USD")
    first = await service.generate(ph.THESIS_ID)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(estimated_notional_account_currency=None)
        )
    second = await service.generate(SECOND_THESIS)
    assert first.created and second.created and fx.calls == 0
    assert second.evaluation_id is not None
    async with clean_tables.session() as session:
        evaluation = await session.get(RiskEvaluation, second.evaluation_id)
        assert evaluation is not None
        assert Decimal(evaluation.snapshot["inputs"]["reserved"]["reserved_notional"]) == Decimal(
            "500"
        )


@pytest.mark.parametrize("wallet", [True, False])
async def test_position_exposure_converts_only_when_not_already_in_account_currency(
    clean_tables: Database,
    wallet: bool,
) -> None:
    service, fx, _ = await setup(clean_tables, "USD", "GBP")
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=service.broker,
                account_id=ph.ACCOUNT_ID,
                broker_ticker="AAPL_US_EQ",
                quantity=Decimal("4"),
                quantity_available=Decimal("4"),
                current_price=Decimal("100"),
                currency="GBP",
                last_synced_at=utcnow(),
                raw={"wallet_impact": {"current_value": "500"}} if wallet else {},
            )
        )
    generated = await service.generate(ph.THESIS_ID)
    assert generated.created and generated.proposal_id is not None
    verdict = await service.revalidate(generated.proposal_id)
    assert verdict is not None and verdict.allowed and verdict.account is not None
    position = verdict.account.position("AAPL_US_EQ")
    assert position is not None and position.market_value == Decimal("500")
    assert fx.calls == 2
    concentration = next(r for r in verdict.rules if r.rule_id == "max_position_concentration")
    assert Decimal(concentration.observed or "0") == Decimal("500")


async def test_active_exposure_limit_uses_the_converted_reservation(clean_tables: Database) -> None:
    service, _, _ = await setup(
        clean_tables, "USD", "GBP", cash=Decimal("1000"), cap=Decimal("800")
    )
    service.config = replace(service.config, max_active_proposal_exposure_pct=Decimal("0.8"))
    first = await service.generate(ph.THESIS_ID)
    assert first.created  # Six shares costing 600 GBP / 750 USD.
    second = await service.generate(SECOND_THESIS)
    assert not second.created and second.evaluation_id is not None
    async with clean_tables.session() as session:
        evaluation = await session.get(RiskEvaluation, second.evaluation_id)
        assert evaluation is not None
        cap = next(r for r in evaluation.rules if r["rule_id"] == "active_proposal_exposure")
        assert Decimal(cap["max_notional"]) == Decimal("50")


async def test_an_unvalued_foreign_position_cannot_be_treated_as_zero_exposure(
    clean_tables: Database,
) -> None:
    service, _, _ = await setup(clean_tables, "USD", "GBP")
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=service.broker,
                account_id=ph.ACCOUNT_ID,
                broker_ticker="AAPL_US_EQ",
                quantity=Decimal("4"),
                quantity_available=Decimal("4"),
                current_price=None,
                currency="GBP",
                last_synced_at=utcnow(),
                raw={},
            )
        )
    generated = await service.generate(ph.THESIS_ID)
    assert not generated.created and generated.evaluation_id is not None
    async with clean_tables.session() as session:
        evaluation = await session.get(RiskEvaluation, generated.evaluation_id)
        assert evaluation is not None
        assert any(
            r["rule_id"] == "current_position" and r["outcome"] == "BLOCK" for r in evaluation.rules
        )


async def test_database_rounding_cannot_reduce_a_same_currency_reservation(
    clean_tables: Database,
) -> None:
    service, _, market = await setup(clean_tables, "USD", "USD")
    market.bid, market.ask = Decimal("100.00004"), Decimal("100.00006")
    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(SECOND_THESIS)
    assert first.proposal_id is not None and second.evaluation_id is not None
    async with clean_tables.session() as session:
        row = await session.get(TradeProposal, first.proposal_id)
        evaluation = await session.get(RiskEvaluation, second.evaluation_id)
        assert row is not None and evaluation is not None
        exact_cost = row.proposed_quantity * row.reference_price
        reserved = Decimal(evaluation.snapshot["inputs"]["reserved"]["reserved_notional"])
        assert row.estimated_notional < exact_cost
        assert reserved >= exact_cost
        assert reserved == row.estimated_notional_account_currency
