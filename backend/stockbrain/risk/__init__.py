"""Deterministic portfolio risk, sizing and the rules that gate both.

Nothing in this package calls an LLM, and nothing in it accepts a quantity from
one.  The research layer contributes exactly two scalars -- an action and a
confidence -- and confidence may only ever shrink a size that the hard limits
already permit.  A ``BLOCK`` cannot be lifted by any of it.
"""

from __future__ import annotations

from stockbrain.risk.config import RiskConfig, SpreadPolicy, risk_config_from_settings
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import (
    AccountState,
    InstrumentIdentity,
    PositionState,
    QuoteSnapshot,
    ReservedExposure,
    RiskDecision,
    RiskInputs,
    RuleResult,
    SizingResult,
)
from stockbrain.risk.spread import SpreadAssessment, assess_spread

__all__ = [
    "AccountState",
    "InstrumentIdentity",
    "PositionState",
    "QuoteSnapshot",
    "ReservedExposure",
    "RiskConfig",
    "RiskDecision",
    "RiskEngine",
    "RiskInputs",
    "RuleResult",
    "SizingResult",
    "SpreadAssessment",
    "SpreadPolicy",
    "assess_spread",
    "risk_config_from_settings",
]
