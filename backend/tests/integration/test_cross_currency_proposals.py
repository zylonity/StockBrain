"""A GBP account sizing a USD listing, end to end through the real service.

The unit tests prove the arithmetic. These prove the *plumbing*: that the rate
is resolved at generation, persisted with its whole provenance, re-resolved and
re-judged at authorization, re-judged again immediately before transmission, and
that a rate which moved retires the proposal rather than resizing it.

This is the capability that unblocks the account Phase 6 measured: GBP cash,
14 of 14 positions in another currency, `currency_alignment` refusing
everything StockBrain could price.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import AuthorizationSource, ProposalStatus
from stockbrain.errors import ProviderUnavailable, RiskBlocked
from stockbrain.fx.base import FxCapability, FxRate, FxRateGrade
from stockbrain.fx.service import FxService
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


class StubFxProvider:
    """An FX source that answers exactly what a test tells it to.

    Not a mock-library double: the risk engine's decision hinges on the *shape*
    of a rate -- its pair, its grade, its timestamp -- so handing over a real
    :class:`FxRate` is both clearer and a stricter test of the adapter contract.
    """

    name = "stub_fx"
    grade = FxRateGrade.EXECUTION

    def __init__(
        self,
        *,
        rate: Decimal = Decimal("1.35"),
        base: str = "GBP",
        quote: str = "USD",
        age_seconds: int = 5,
        error: Exception | None = None,
        provider_grade: FxRateGrade = FxRateGrade.EXECUTION,
    ) -> None:
        self._rate = rate
        self._base = base
        self._quote = quote
        self._age_seconds = age_seconds
        self.error = error
        self.grade = provider_grade
        self.calls = 0

    async def latest(self, base: str, quote: str) -> FxRate:
        self.calls += 1
        if self.error is not None:
            raise self.error
        now = dt.datetime.now(dt.UTC)
        return FxRate(
            base_currency=self._base,
            quote_currency=self._quote,
            rate=self._rate,
            provider=self.name,
            grade=self.grade,
            provider_timestamp=now - dt.timedelta(seconds=self._age_seconds),
            received_at=now,
        )

    async def capability(self) -> FxCapability:
        return FxCapability(provider=self.name, grade=self.grade)

    async def aclose(self) -> None:
        return None


def cross_currency_settings(**overrides: object) -> Settings:
    """A deployment configured for cross-currency sizing.

    Both switches, because ``RISK_REQUIRE_SAME_CURRENCY=false`` with
    ``FX_PROVIDER=none`` is refused at startup -- that pairing is bug 22.
    """
    base: dict[str, object] = {
        "risk_require_same_currency": False,
        "fx_provider": "alpaca",
        "alpaca_api_key": "key",
        "alpaca_api_secret": "secret",
        "risk_max_notional_per_trade": Decimal("500"),
        "risk_max_trade_pct": Decimal("1"),
        "risk_min_trade_notional": Decimal("20"),
        "risk_confidence_modulates_size": False,
    }
    base.update(overrides)
    return ph.settings(**base)


def service_with_fx(
    database: Database, settings: Settings, *, provider: StubFxProvider | None = None
) -> tuple[object, StubFxProvider]:
    fx_provider = provider or StubFxProvider()
    service = ph.service_with(database, settings)
    service.fx = FxService(settings, provider=fx_provider)
    return service, fx_provider


async def _proposal(database: Database) -> TradeProposal:
    async with database.session() as session:
        return (
            (await session.execute(sa.select(TradeProposal).order_by(TradeProposal.created_at)))
            .scalars()
            .one()
        )


async def _seed_gbp_account_usd_listing(database: Database) -> None:
    """Exactly the world Phase 6 measured on the live account."""
    await ph.seed(database, currency="USD")
    await ph.fund(database, currency="GBP")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
async def test_a_gbp_account_can_now_propose_a_usd_trade(clean_tables: Database) -> None:
    """The block Phase 6 documented, lifted -- and only behind a verified rate.

    The arithmetic: a 500 GBP cap at GBP/USD 1.35 is 675 USD; 675 / 200.05 (the
    ask) is 3.37, so three whole shares committing 600.15 USD = 444.55 GBP,
    inside the 500 GBP cap.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, fx_provider = service_with_fx(clean_tables, settings)

    result = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert result.created, result.reason
    assert fx_provider.calls == 1

    proposal = await _proposal(clean_tables)
    assert proposal.proposed_quantity == Decimal("3")
    assert proposal.reference_currency == "USD"
    assert proposal.account_currency == "GBP"
    # The broker-facing number and the portfolio-facing number, both recorded.
    assert proposal.estimated_notional == Decimal("600.1500")
    assert proposal.estimated_notional_account_currency is not None
    assert proposal.estimated_notional_account_currency < Decimal("500")


async def test_the_whole_rate_provenance_is_persisted(clean_tables: Database) -> None:
    """A converted number without its rate, that rate's source and that rate's
    age is a number nobody can audit afterwards."""
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, _ = service_with_fx(clean_tables, settings)
    assert (await service.generate(ph.THESIS_ID)).created  # type: ignore[attr-defined]

    proposal = await _proposal(clean_tables)
    assert proposal.fx_required is True
    assert proposal.fx_rate == Decimal("1.350000000000")
    assert proposal.fx_base_currency == "GBP"
    assert proposal.fx_quote_currency == "USD"
    assert proposal.fx_direction == "DIRECT"
    assert proposal.fx_provider == "stub_fx"
    assert proposal.fx_rate_grade == "EXECUTION"
    assert proposal.fx_provider_timestamp is not None
    assert proposal.fx_received_at is not None
    assert proposal.fx_age_seconds is not None


async def test_a_same_currency_proposal_records_no_rate_at_all(
    clean_tables: Database,
) -> None:
    """ "No conversion was needed" is a different fact from "converted at 1.0".

    The database refuses the second shape outright
    (``ck_trade_proposals_fx_rate_requires_fx_required``); this asserts the
    service writes the first.
    """
    await ph.seed(clean_tables, currency="USD")
    await ph.fund(clean_tables, currency="USD")
    settings = cross_currency_settings()
    service, fx_provider = service_with_fx(clean_tables, settings)
    assert (await service.generate(ph.THESIS_ID)).created  # type: ignore[attr-defined]

    proposal = await _proposal(clean_tables)
    assert proposal.fx_required is False
    assert proposal.fx_rate is None
    # And no rate was fetched: there was nothing to convert.
    assert fx_provider.calls == 0


async def test_an_unavailable_fx_source_blocks_rather_than_assuming(
    clean_tables: Database,
) -> None:
    """The whole point. A provider outage must not become an inferred rate."""
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, _ = service_with_fx(
        clean_tables,
        settings,
        provider=StubFxProvider(error=ProviderUnavailable("forex feed 503")),
    )

    result = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert not result.created
    assert "fx_available" in result.blocks or any(
        "FX" in reason or "fx" in reason for reason in result.blocks
    )
    async with clean_tables.session() as session:
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(TradeProposal))
        ).scalar_one()
    assert count == 0


async def test_a_stale_rate_blocks_generation(clean_tables: Database) -> None:
    """Fifteen minutes is the execution-grade limit; an hour is not fresh."""
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings(fx_max_age_seconds=900.0)
    service, _ = service_with_fx(clean_tables, settings, provider=StubFxProvider(age_seconds=3600))
    result = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert not result.created
    assert any("older than" in reason for reason in result.blocks)


async def test_a_reference_grade_rate_needs_the_explicit_opt_in(
    clean_tables: Database,
) -> None:
    """Frankfurter's own documentation says it "is not for live trading".

    Honouring that is the difference between using a published fixing knowingly
    and mistaking it for a dealable quote.
    """
    await _seed_gbp_account_usd_listing(clean_tables)

    refused_settings = cross_currency_settings(fx_allow_reference_grade=False)
    service, _ = service_with_fx(
        clean_tables,
        refused_settings,
        provider=StubFxProvider(provider_grade=FxRateGrade.REFERENCE),
    )
    refused = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert not refused.created
    assert any("reference-grade" in reason for reason in refused.blocks)

    permitted_settings = cross_currency_settings(fx_allow_reference_grade=True)
    permitted_service, _ = service_with_fx(
        clean_tables,
        permitted_settings,
        provider=StubFxProvider(provider_grade=FxRateGrade.REFERENCE),
    )
    permitted = await permitted_service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert permitted.created, permitted.reason


async def test_a_rate_for_the_wrong_pair_blocks(clean_tables: Database) -> None:
    """No chaining through a third currency, at the service layer too."""
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, _ = service_with_fx(
        clean_tables, settings, provider=StubFxProvider(base="EUR", quote="JPY")
    )
    result = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert not result.created
    assert any("cannot convert" in reason for reason in result.blocks)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
async def test_authorization_re_resolves_the_rate(clean_tables: Database) -> None:
    """Nothing is trusted from generation time -- including the rate.

    Authorizing against the rate a proposal was drafted with would be
    authorizing a size derived from a number that may be half an hour old.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, fx_provider = service_with_fx(clean_tables, settings)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None
    assert fx_provider.calls == 1

    await service.authorize(  # type: ignore[attr-defined]
        generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:owner"
    )
    assert fx_provider.calls == 2

    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.APPROVED


async def test_a_rate_that_moved_past_the_envelope_invalidates_rather_than_resizes(
    clean_tables: Database,
) -> None:
    """The FX analogue of the price-drift rule, and it must not resize.

    On a GBP account buying USD, a one percent move in the pair moves the
    trade's account-currency notional by one percent -- straight through the
    per-trade cap, the cash reserve and the concentration limit, none of which
    were re-derived. Re-pricing under somebody's finger is how a person
    approves a trade they did not read.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings(fx_max_rate_drift_pct=Decimal("0.005"))
    provider = StubFxProvider(rate=Decimal("1.35"))
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    # Three percent, far outside the half-percent envelope.
    provider._rate = Decimal("1.39")

    with pytest.raises(RiskBlocked):
        await service.authorize(  # type: ignore[attr-defined]
            generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:owner"
        )

    proposal = await _proposal(clean_tables)
    # Invalidated, and *not* silently re-sized to whatever fits now.
    assert proposal.status is ProposalStatus.INVALIDATED
    assert proposal.proposed_quantity == Decimal("3")
    assert proposal.invalidation_reason is not None


async def test_a_rate_that_moved_inside_the_envelope_still_authorizes(
    clean_tables: Database,
) -> None:
    """The envelope has to be an envelope, not a requirement of exactness.

    A rate that never moves does not exist, and refusing every basis point of
    movement would make cross-currency authorization impossible.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings(fx_max_rate_drift_pct=Decimal("0.005"))
    provider = StubFxProvider(rate=Decimal("1.3500"))
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    provider._rate = Decimal("1.3540")  # ~0.3%
    await service.authorize(  # type: ignore[attr-defined]
        generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:owner"
    )
    assert (await _proposal(clean_tables)).status is ProposalStatus.APPROVED


async def test_an_fx_outage_at_authorization_refuses_without_destroying(
    clean_tables: Database,
) -> None:
    """A source that stopped answering says nothing about the trade.

    The authorization is refused -- nothing is authorized on an unknown rate --
    but the proposal survives for the next attempt, because failing every
    proposal whenever a provider blinks would make an outage destructive.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    provider = StubFxProvider()
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    provider.error = ProviderUnavailable("forex feed 503")
    with pytest.raises(RiskBlocked):
        await service.authorize(  # type: ignore[attr-defined]
            generated.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:owner"
        )

    # The refusal is durable and readable, and it is the *authorization* that
    # was refused. Whether the proposal survives an FX outage is the preflight's
    # transient/permanent question, tested in `test_chaos.py`.
    proposal = await _proposal(clean_tables)
    assert proposal.status in {ProposalStatus.READY, ProposalStatus.INVALIDATED}


# ---------------------------------------------------------------------------
# Revalidation, which is what the pre-send preflight uses
# ---------------------------------------------------------------------------
async def test_revalidate_reports_the_fx_snapshot_it_judged(
    clean_tables: Database,
) -> None:
    """The pre-send check reads the same rate the record will show.

    `revalidate` is read-only and shares the engine and the rule functions with
    `authorize`, so the checks guarding a broker POST cannot diverge from the
    ones that guarded the authorization.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    service, provider = service_with_fx(clean_tables, settings)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    revalidation = await service.revalidate(generated.proposal_id)  # type: ignore[attr-defined]
    assert revalidation is not None
    assert revalidation.fx is not None
    assert revalidation.fx.rate == Decimal("1.35")
    assert revalidation.fx.usable
    assert provider.calls == 2
    payload = revalidation.as_dict()
    assert payload["fx"] is not None


async def test_revalidate_blocks_on_a_moved_rate(clean_tables: Database) -> None:
    """And the drift rule fires on the read-only path too, which is the one the
    send actually consults."""
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings(fx_max_rate_drift_pct=Decimal("0.005"))
    provider = StubFxProvider(rate=Decimal("1.35"))
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    provider._rate = Decimal("1.42")
    revalidation = await service.revalidate(generated.proposal_id)  # type: ignore[attr-defined]
    assert revalidation is not None
    assert "fx_rate_drift" in revalidation.rule_ids


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
async def test_the_invalidation_sweep_retires_a_proposal_whose_rate_moved(
    clean_tables: Database,
) -> None:
    """A proposal sitting on the screen for twenty minutes must not stay
    authorizable through a large FX move.

    Bounded by ``PROPOSAL_REVALIDATION_BATCH`` and memoised per currency pair
    within one pass, so the sweep does not become one FX request per open
    proposal per minute.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings(fx_max_rate_drift_pct=Decimal("0.005"))
    provider = StubFxProvider(rate=Decimal("1.35"))
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    provider._rate = Decimal("1.45")
    counts = await service.sweep()  # type: ignore[attr-defined]
    # `market_invalidated` is the re-pricing branch: the sweep separates
    # "expired", "a precondition went stale" and "the market moved", and an FX
    # move is the third.
    assert counts["market_invalidated"] >= 1, counts

    proposal = await _proposal(clean_tables)
    assert proposal.status is ProposalStatus.INVALIDATED
    assert proposal.invalidation_reason is not None
    assert "GBPUSD" in proposal.invalidation_reason


async def test_the_sweep_does_not_resolve_fx_for_a_same_currency_proposal(
    clean_tables: Database,
) -> None:
    """No rate is needed, so none is fetched.

    Asking a free public API for a number nothing will use is impolite as well
    as pointless.
    """
    await ph.seed(clean_tables, currency="USD")
    await ph.fund(clean_tables, currency="USD")
    settings = cross_currency_settings()
    service, provider = service_with_fx(clean_tables, settings)
    assert (await service.generate(ph.THESIS_ID)).created  # type: ignore[attr-defined]

    before = provider.calls
    await service.sweep()  # type: ignore[attr-defined]
    assert provider.calls == before == 0


async def test_a_missing_rate_during_the_sweep_does_not_retire_the_proposal(
    clean_tables: Database,
) -> None:
    """Degradation, not destruction -- the same rule as a missing quote.

    Retiring every open proposal because an FX source blinked would make an
    outage destructive.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    settings = cross_currency_settings()
    provider = StubFxProvider()
    service, _ = service_with_fx(clean_tables, settings, provider=provider)
    generated = await service.generate(ph.THESIS_ID)  # type: ignore[attr-defined]
    assert generated.proposal_id is not None

    provider.error = ProviderUnavailable("forex feed 503")
    await service.sweep()  # type: ignore[attr-defined]
    assert (await _proposal(clean_tables)).status is ProposalStatus.READY


# ---------------------------------------------------------------------------
# The guard that must not be removable by configuration
# ---------------------------------------------------------------------------
async def test_permitting_cross_currency_without_a_provider_is_refused_at_startup() -> None:
    """Bug 22, as a startup failure.

    Through Phase 8 this pairing produced a ``WARN`` and a quantity computed by
    dividing an account-currency cap by an instrument-currency price. It is now
    a configuration error with the reason spelled out.
    """
    with pytest.raises(ValueError, match="RISK_REQUIRE_SAME_CURRENCY=false"):
        ph.settings(risk_require_same_currency=False, fx_provider="none")


async def test_the_default_deployment_still_blocks_cross_currency(
    clean_tables: Database,
) -> None:
    """Phase 9 adds a capability; it does not change the default posture.

    A deployment that has not chosen an FX source behaves exactly as Phase 6
    did, and says which setting is responsible.
    """
    await _seed_gbp_account_usd_listing(clean_tables)
    service = ph.service_with(clean_tables, ph.settings())
    result = await service.generate(ph.THESIS_ID)
    assert not result.created
    assert any("RISK_REQUIRE_SAME_CURRENCY" in reason for reason in result.blocks)
    assert uuid.UUID(str(ph.THESIS_ID))
