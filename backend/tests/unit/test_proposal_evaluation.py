"""The common evaluator must preserve the engine's complete, ordered verdict.

Database races remain integration tests. Here only the reservation read is
stubbed; every risk rule and sizing calculation is real, with fixed observations
so we can compare the entire decision rather than just one blocking rule.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.broker.account_state import AccountStateService
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import Broker, OrderSide, RuleOutcome
from stockbrain.fx.service import FxService
from stockbrain.proposals.evaluation import EvaluationContext, EvaluationFacts, ProposalEvaluator
from stockbrain.proposals.quotes import QuoteFetcher
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import ReservedExposure, RiskInputs
from tests import proposal_helpers as ph
from tests import risk_helpers as h


def evaluator(inputs: RiskInputs) -> ProposalEvaluator:
    settings = ph.settings()
    database = Database(settings)
    return ProposalEvaluator(
        database,
        settings,
        config=inputs.config,
        account_state=AccountStateService(database),
        quotes=QuoteFetcher(None),
        fx=FxService(settings, provider=None),
        broker=Broker.TRADING212,
        engine=RiskEngine(),
    )


def proposal(inputs: RiskInputs) -> TradeProposal:
    return TradeProposal(
        id=uuid.uuid4(),
        account_id="12345",
        side=OrderSide.BUY,
        proposed_quantity=Decimal("1"),
        reference_price=Decimal("200.05"),
        expires_at=h.NOW + dt.timedelta(minutes=30),
        risk_policy_version=inputs.config.version,
        fx_rate=inputs.authorized_fx_rate,
        fx_required=inputs.fx is not None and inputs.fx.conversion_required,
    )


CASES = [
    pytest.param(h.inputs(), None, id="healthy"),
    pytest.param(
        h.inputs(state=h.account(captured_at=h.NOW - dt.timedelta(hours=1))),
        "account_state_freshness",
        id="stale-account",
    ),
    pytest.param(h.inputs(state=None), "account_state_available", id="missing-account"),
    pytest.param(h.inputs(snapshot=h.quote(age_ms=60_000)), "quote_freshness", id="stale-quote"),
    pytest.param(
        h.inputs(snapshot=h.quote(bid=Decimal("180"), ask=Decimal("220"))),
        "spread_ceiling",
        id="wide-spread",
    ),
    pytest.param(
        h.inputs(
            state=h.account(currency="GBP"), risk_config=h.config(require_same_currency=False)
        ),
        "fx_available",
        id="missing-fx",
    ),
    pytest.param(
        h.inputs(
            state=h.account(currency="GBP"),
            risk_config=h.config(require_same_currency=False),
            fx=h.fx_snapshot(rate=Decimal("1.42")),
            authorized_fx_rate=Decimal("1.35"),
        ),
        "fx_rate_drift",
        id="fx-drift",
    ),
    pytest.param(h.inputs(confidence=Decimal("0.1")), "research_confidence_floor", id="confidence"),
    *[
        pytest.param(
            h.inputs(
                reserved=ReservedExposure(
                    count=1, same_instrument_count=1, same_instrument_sides=(side,)
                )
            ),
            "duplicate_or_conflicting_proposal",
            id=f"reserved-{side.lower()}",
        )
        for side in ("BUY", "SELL")
    ],
    pytest.param(
        h.inputs(risk_config=h.config(max_notional_per_trade=Decimal("1"))),
        None,
        id="stricter-config",
    ),
]


@pytest.mark.parametrize("inputs,blocked_rule", CASES)
@pytest.mark.parametrize("existing", [False, True], ids=["generation", "existing-proposal"])
async def test_the_shared_path_preserves_the_complete_engine_decision(
    inputs: RiskInputs, blocked_rule: str | None, existing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = evaluator(inputs)
    row = proposal(inputs) if existing else None
    # A new proposal has no historical FX reference to compare against.
    expected_inputs = inputs if existing else replace(inputs, authorized_fx_rate=None)
    expected = RiskEngine().evaluate(expected_inputs, now=h.NOW)
    reservations = AsyncMock(return_value=inputs.reserved)
    monkeypatch.setattr(service, "_reserved_exposure", reservations)
    async with AsyncSession() as session:
        verdict = await service.evaluate(
            session,
            EvaluationContext(inputs.action, inputs.confidence, inputs.identity, "12345", row),
            EvaluationFacts(
                inputs.account,
                inputs.quote,
                inputs.fx,
                inputs.account_state_missing_reason,
                inputs.quote_missing_reason,
            ),
            now=h.NOW,
        )
        reservations.assert_awaited_once_with(
            session,
            "12345",
            inputs.identity.broker_ticker,
            account_currency=inputs.account.currency if inputs.account else None,
            exclude_proposal_id=row.id if row is not None else None,
        )
    assert verdict.decision == expected
    assert verdict.rules[: len(expected.rules)] == expected.rules
    assert verdict.decision.snapshot_hash() == expected.snapshot_hash()
    if blocked_rule is not None and (existing or blocked_rule != "fx_rate_drift"):
        assert blocked_rule in verdict.rule_ids
    if existing:
        assert [rule.rule_id for rule in verdict.rules[len(expected.rules) :]] == [
            "proposal_ttl",
            "reference_price_drift",
            "fx_rate_drift",
            "risk_policy_version",
            "authorization_envelope",
        ]
    else:
        assert verdict.rules == expected.rules


@pytest.mark.parametrize("change", ["price", "fx", "policy", "expiry", "quantity"])
async def test_existing_proposal_constraints_are_shared_with_market_review(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = h.inputs()
    row = proposal(inputs)
    expected_rule = {
        "price": "reference_price_drift",
        "fx": "fx_rate_drift",
        "policy": "risk_policy_version",
        "expiry": "proposal_ttl",
        "quantity": "authorization_envelope",
    }[change]
    if change == "price":
        inputs = replace(inputs, quote=h.quote(bid=Decimal("249.95"), ask=Decimal("250.05")))
    elif change == "fx":
        inputs = replace(
            inputs,
            account=h.account(currency="GBP"),
            fx=h.fx_snapshot(rate=Decimal("1.45")),
            config=h.config(require_same_currency=False),
        )
        row.fx_required = True
        row.fx_rate = Decimal("1.35")
        row.risk_policy_version = inputs.config.version
    elif change == "policy":
        inputs = replace(inputs, config=h.config(max_notional_per_trade=Decimal("123")))
    elif change == "expiry":
        row.expires_at = h.NOW
    else:
        row.proposed_quantity = Decimal("10000")
    service = evaluator(inputs)
    monkeypatch.setattr(service, "_reserved_exposure", AsyncMock(return_value=ReservedExposure()))
    async with AsyncSession() as session:
        verdict = await service.evaluate(
            session,
            EvaluationContext(inputs.action, inputs.confidence, inputs.identity, "12345", row),
            EvaluationFacts(inputs.account, inputs.quote, inputs.fx, None, None),
            now=h.NOW,
        )
    assert expected_rule in verdict.rule_ids
    if change in {"price", "fx"}:
        failure = service.market_failure(row, inputs.quote, None, inputs.fx, h.NOW)
        assert failure == next(
            rule.reason for rule in verdict.blocked if rule.rule_id == expected_rule
        )
    assert all(rule.outcome is not RuleOutcome.PASS for rule in verdict.blocked)


async def test_a_missing_listing_loads_account_then_fx_without_a_quote_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = evaluator(h.inputs())
    calls: list[str] = []

    async def load_account(**kwargs: object) -> tuple[None, str]:
        calls.append("account")
        assert kwargs == {
            "max_age_seconds": service.config.max_account_state_age_seconds,
            "now": h.NOW,
        }
        return None, "stale account"

    async def load_fx(**kwargs: object) -> None:
        calls.append("fx")
        assert kwargs == {"account": None, "instrument_currency": None, "now": h.NOW}

    quote_fetch = AsyncMock()
    monkeypatch.setattr(service.account_state, "load", load_account)
    monkeypatch.setattr(service.quotes, "fetch", quote_fetch)
    monkeypatch.setattr(service, "fx_snapshot", load_fx)
    facts = await service.load(None, instrument_currency=None, now=h.NOW)
    assert calls == ["account", "fx"]
    quote_fetch.assert_not_awaited()
    assert facts.account_reason == "stale account"
    assert facts.quote_reason == "the listing could not be identified for pricing"


async def test_observations_are_loaded_in_account_quote_fx_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from stockbrain.db.models.companies import BrokerInstrument

    service = evaluator(h.inputs())
    calls: list[str] = []
    account, quote, fx = h.account(), h.quote(), h.inputs().fx
    instrument = BrokerInstrument(id=h.INSTRUMENT_ID, currency="USD")

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[AsyncSession]:
        async with AsyncSession() as session:
            yield session

    async def load_account(**kwargs: object) -> tuple[object, None]:
        calls.append("account")
        return account, None

    async def load_quote(*args: object, **kwargs: object) -> tuple[object, None]:
        calls.append("quote")
        assert args[1:] == (instrument, service.config)
        assert kwargs == {"now": h.NOW}
        return quote, None

    async def load_fx(**kwargs: object) -> object:
        calls.append("fx")
        assert kwargs == {"account": account, "instrument_currency": "USD", "now": h.NOW}
        return fx

    monkeypatch.setattr(service.database, "session", session_scope)
    monkeypatch.setattr(service.account_state, "load", load_account)
    monkeypatch.setattr(service.quotes, "fetch", load_quote)
    monkeypatch.setattr(service, "fx_snapshot", load_fx)
    assert await service.load(instrument, instrument_currency="USD", now=h.NOW) == EvaluationFacts(
        account, quote, fx, None, None
    )
    assert calls == ["account", "quote", "fx"]


def test_proposals_have_one_risk_input_construction_and_one_engine_call() -> None:
    """Catch a future entry point recreating the safety sequence locally."""
    import ast
    from pathlib import Path

    import stockbrain.proposals.evaluation as evaluation_module

    root = Path(evaluation_module.__file__).parent
    constructors: list[str] = []
    engine_calls: list[str] = []
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "RiskInputs":
                constructors.append(path.name)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "evaluate"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "engine"
            ):
                engine_calls.append(path.name)
    assert constructors == engine_calls == ["evaluation.py"]


@pytest.mark.parametrize("missing", ["quote", "fx", "both"])
def test_market_review_keeps_outages_non_destructive(missing: str) -> None:
    inputs = h.inputs(
        state=h.account(currency="GBP"),
        risk_config=h.config(require_same_currency=False),
        fx=h.fx_snapshot(),
        authorized_fx_rate=Decimal("1.35"),
    )
    service = evaluator(inputs)
    row = proposal(inputs)
    quote = None if missing in {"quote", "both"} else inputs.quote
    fx = None if missing in {"fx", "both"} else inputs.fx
    assert service.market_failure(row, quote, "quote unavailable", fx, h.NOW) is None


def test_market_review_preserves_fx_before_spread_before_price_refusal_order() -> None:
    inputs = h.inputs(
        state=h.account(currency="GBP"),
        risk_config=h.config(require_same_currency=False),
        fx=h.fx_snapshot(rate=Decimal("1.45")),
        authorized_fx_rate=Decimal("1.35"),
        snapshot=h.quote(bid=Decimal("240"), ask=Decimal("260")),
    )
    service = evaluator(inputs)
    row = proposal(inputs)
    failure = service.market_failure(row, inputs.quote, None, inputs.fx, h.NOW)
    assert failure is not None and "GBPUSD" in failure
    failure = service.market_failure(row, inputs.quote, None, h.fx_snapshot(), h.NOW)
    assert failure is not None and "widened" in failure
