"""The deterministic risk engine.

A pure function from :class:`~stockbrain.risk.models.RiskInputs` to
:class:`~stockbrain.risk.models.RiskDecision`.  No database, no HTTP, no clock
of its own, no model output beyond an action and a bounded confidence scalar.
That is what makes the system's central safety claim testable: *blocked risk
never reaches the broker* is a property of a function, not of a code path
somebody remembered to call.

The evaluation order is fixed:

1. **Gates.**  Identity, account state, quote provenance, freshness, spread,
   session, currency, FX availability and freshness, position, duplicate
   proposals, confidence floor.  Any
   ``BLOCK`` here ends the decision; nothing downstream can revive it.
2. **Caps.**  Each computes a notional ceiling from the snapshot.  The engine
   takes the minimum.  A cap with no headroom becomes a block.
3. **Reductions.**  Research confidence and, under an explicit policy, a wide
   spread may scale the size down inside the caps.  Multiplicatively, always in
   ``(0, 1]``, never upward.
4. **Sizing.**  Ordinary arithmetic producing a side, a quantity and a
   reference price.

A ``BLOCK`` is absolute by construction: the final outcome is computed from the
rule list, and no later stage may remove a rule from it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from stockbrain.db.base import utcnow
from stockbrain.enums import RiskOutcome, RuleOutcome
from stockbrain.risk.models import ZERO, RiskDecision, RiskInputs, RuleResult, SizingResult
from stockbrain.risk.rules import (
    cap_rule_results,
    confidence_size_factor,
    gate_results,
    notional_caps,
)
from stockbrain.risk.sizing import size_trade

__all__ = ["RiskEngine"]


class RiskEngine:
    """Evaluates one candidate trade against the configured limits."""

    def evaluate(self, inputs: RiskInputs, *, now: dt.datetime | None = None) -> RiskDecision:
        evaluated_at = now or utcnow()
        rules: list[RuleResult] = list(gate_results(inputs))

        caps = notional_caps(inputs)
        intended = min((cap.limit for cap in caps if cap.defines_intent), default=ZERO)
        rules.extend(cap_rule_results(caps, intended))

        confidence_rule = confidence_size_factor(inputs)
        if confidence_rule is not None:
            rules.append(confidence_rule)

        blocked = any(rule.outcome is RuleOutcome.BLOCK for rule in rules)

        if blocked:
            # Sizing is not attempted at all. A blocked decision that still
            # carried a quantity would be one refactor away from being acted on.
            sizing = SizingResult(
                side=None,
                quantity=ZERO,
                target_notional=ZERO,
                max_quantity=ZERO,
                max_notional=ZERO,
                reference_price=None,
                currency=inputs.identity.currency,
                reasons=("risk blocked this trade; no size was computed",),
                executable=False,
            )
            return self._decide(inputs, rules, sizing, RiskOutcome.BLOCK, evaluated_at)

        effective_cap = min((cap.limit for cap in caps), default=ZERO)
        size_factor = _combined_size_factor(rules)
        sizing = size_trade(
            action=inputs.action,
            config=inputs.config,
            identity=inputs.identity,
            quote=inputs.quote,
            account=inputs.account,
            max_notional=effective_cap,
            size_factor=size_factor,
            fx=inputs.fx,
        )

        reduced = any(rule.outcome is RuleOutcome.REDUCE for rule in rules)
        outcome = RiskOutcome.REDUCE_SIZE if reduced else RiskOutcome.ALLOW
        return self._decide(inputs, rules, sizing, outcome, evaluated_at)

    # ------------------------------------------------------------------
    def _decide(
        self,
        inputs: RiskInputs,
        rules: list[RuleResult],
        sizing: SizingResult,
        outcome: RiskOutcome,
        evaluated_at: dt.datetime,
    ) -> RiskDecision:
        return RiskDecision(
            outcome=outcome,
            policy_version=inputs.config.version,
            rules=tuple(rules),
            sizing=sizing,
            inputs_summary={
                "action": inputs.action.value,
                "confidence": str(inputs.confidence),
                "identity": inputs.identity.as_dict(),
                "account": inputs.account.as_dict() if inputs.account else None,
                "quote": inputs.quote.as_dict() if inputs.quote else None,
                "fx": inputs.fx.as_dict() if inputs.fx else None,
                "reserved": inputs.reserved.as_dict(),
                "config": inputs.config.as_dict(),
                "now": inputs.now.isoformat(),
            },
            evaluated_at=evaluated_at,
        )


def _combined_size_factor(rules: list[RuleResult]) -> Decimal:
    """Multiply every reduction factor the rules imposed.

    Clamped into ``(0, 1]``: a factor above one would let a rule *increase* a
    size, which no rule is permitted to do.
    """
    factor = Decimal(1)
    for rule in rules:
        if rule.size_factor is None:
            continue
        factor *= min(Decimal(1), max(ZERO, rule.size_factor))
    return factor
