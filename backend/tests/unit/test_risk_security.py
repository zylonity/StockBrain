"""What a model can and cannot reach through the risk and proposal layers.

The claim this file defends is narrow and load-bearing: **research contributes
an action and a confidence, and nothing else.** No quantity, no price, no
instrument, no permission. Each test states one way that could be violated and
shows it cannot be.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.enums import ResolutionStatus, RiskOutcome, RuleOutcome
from stockbrain.intelligence.research import ResearchDecision
from stockbrain.risk import config as risk_config_module
from stockbrain.risk import engine as risk_engine_module
from stockbrain.risk import rules as rules_module
from stockbrain.risk import sizing as sizing_module
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import RiskInputs
from tests import risk_helpers as h

ENGINE = RiskEngine()
RISK_MODULES = (risk_engine_module, rules_module, sizing_module, risk_config_module)


# ---------------------------------------------------------------------------
# The research contract carries no execution fields
# ---------------------------------------------------------------------------
def test_a_research_decision_has_no_quantity_or_order_field() -> None:
    """Enforced by the schema, not by convention."""
    fields = set(ResearchDecision.model_fields)
    assert not fields & {
        "quantity",
        "proposed_quantity",
        "size",
        "allocation",
        "notional",
        "order",
        "order_type",
        "side",
        "ticker",
        "broker_ticker",
        "limit_price",
        "stop_price",
        "account_id",
        "max_notional",
    }
    assert fields >= {"action", "confidence", "horizon", "thesis"}


def test_a_model_supplied_quantity_cannot_even_be_expressed() -> None:
    """Extra fields are forbidden, so a hallucinated size is rejected, not ignored."""
    payload = {
        "action": "BUY",
        "confidence": 0.9,
        "horizon": "days",
        "thesis": "t",
        "bull_case": "b",
        "bear_case": "b",
        "catalysts": [],
        "risks": [],
        "invalidation_conditions": [],
        "evidence_ids": [],
        "quantity": 10_000,
    }
    with pytest.raises(ValueError):
        ResearchDecision.model_validate(payload)


def test_the_risk_engine_reads_exactly_two_scalars_from_research() -> None:
    """``RiskInputs`` is the whole surface, and it has no free-text field."""
    fields = set(RiskInputs.__dataclass_fields__)
    assert "action" in fields and "confidence" in fields
    assert not fields & {"thesis", "bull_case", "bear_case", "rationale", "reports", "summary"}


# ---------------------------------------------------------------------------
# No LLM path reaches the risk engine or a broker
# ---------------------------------------------------------------------------
def test_no_risk_module_imports_an_llm_or_a_broker_client() -> None:
    """Structural, not aspirational."""
    for module in RISK_MODULES:
        source = inspect.getsource(module).lower()
        for forbidden in ("deepseek", "openai", "langchain", "langgraph", "tradingagents"):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"
        assert "trading212" not in source
        assert "place_order" not in source


def test_the_risk_engine_performs_no_io_at_all() -> None:
    """A pure function is what makes "blocked risk never reaches the broker" testable."""
    source = inspect.getsource(risk_engine_module) + inspect.getsource(rules_module)
    for forbidden in ("httpx", "requests", "await session", "aiohttp", "open("):
        assert forbidden not in source


def test_no_module_under_risk_or_proposals_names_a_broker_order_endpoint() -> None:
    root = Path(inspect.getfile(risk_engine_module)).resolve().parents[1]
    for package in ("risk", "proposals"):
        for path in sorted((root / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "/equity/orders" not in text
            assert "place_order" not in text
            assert "place_market_order" not in text


def test_the_proposal_service_never_receives_a_broker_credential() -> None:
    """It reaches broker data only through already-constructed read-only clients."""
    from stockbrain.proposals.service import ProposalService

    parameters = set(inspect.signature(ProposalService.__init__).parameters)
    assert not parameters & {"api_key", "api_secret", "token", "credentials", "client"}


# ---------------------------------------------------------------------------
# Model output cannot bypass a blocker
# ---------------------------------------------------------------------------
def test_confidence_cannot_lift_any_single_block() -> None:
    blocked_worlds = {
        "identity": h.inputs(
            confidence=Decimal("1.0"),
            instrument=h.identity(resolution_status=ResolutionStatus.AMBIGUOUS),
        ),
        "stale quote": h.inputs(confidence=Decimal("1.0"), snapshot=h.quote(age_ms=10_000_000)),
        "wide spread": h.inputs(
            confidence=Decimal("1.0"),
            snapshot=h.quote(bid=Decimal("100"), ask=Decimal("140")),
        ),
        "no account": h.inputs(confidence=Decimal("1.0"), state=None),
        "cross currency": h.inputs(
            confidence=Decimal("1.0"), instrument=h.identity(currency="JPY")
        ),
    }
    for name, inputs in blocked_worlds.items():
        decision = ENGINE.evaluate(inputs)
        assert decision.outcome is RiskOutcome.BLOCK, name
        assert not decision.allowed, name
        assert decision.sizing.quantity == 0, name


def test_confidence_is_clamped_into_a_bounded_band() -> None:
    """Even an out-of-range confidence can only shrink a size.

    The schema already bounds it to [0, 1]; this is defence in depth against a
    future caller passing something else.
    """
    for value in (Decimal("5"), Decimal("-3"), Decimal("1")):
        decision = ENGINE.evaluate(h.inputs(confidence=value))
        if not decision.allowed:
            continue
        assert decision.sizing.target_notional <= decision.sizing.max_notional


def test_the_size_factor_can_never_exceed_one() -> None:
    from stockbrain.risk.engine import _combined_size_factor
    from stockbrain.risk.models import RuleResult

    rules = [
        RuleResult(
            rule_id="rogue",
            rule_version=1,
            outcome=RuleOutcome.REDUCE,
            reason="a rule trying to enlarge a position",
            size_factor=Decimal("10"),
        )
    ]
    assert _combined_size_factor(rules) == Decimal(1)


# ---------------------------------------------------------------------------
# The execution policy is not a risk override
# ---------------------------------------------------------------------------
def test_the_engine_cannot_see_the_execution_policy_at_all() -> None:
    """Automatic mode changes *who* authorizes, never *what* is permitted."""
    fields = set(RiskInputs.__dataclass_fields__)
    assert "execution_policy" not in fields
    assert "authorization_source" not in fields
    source = inspect.getsource(risk_engine_module) + inspect.getsource(rules_module)
    assert "ExecutionPolicy" not in source
    assert "AuthorizationSource" not in source


def test_untrusted_evidence_cannot_reach_the_risk_configuration() -> None:
    """The config is built from typed settings and nothing else."""
    source = inspect.getsource(risk_config_module)
    for forbidden in ("Event", "Source", "Thesis", "normalized_text", "headline", "packet"):
        assert forbidden not in source
    signature = inspect.signature(risk_config_module.risk_config_from_settings)
    assert list(signature.parameters) == ["settings"]


def test_the_risk_policy_version_is_derived_only_from_the_thresholds() -> None:
    """A version an event could influence would be a version nobody can trust."""
    from stockbrain.risk.config import RiskConfig

    baseline = RiskConfig()
    assert baseline.version == RiskConfig().version
    assert baseline.version != RiskConfig(max_spread_bps=Decimal("49")).version
