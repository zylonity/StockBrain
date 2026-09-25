"""Operator-requested rebalancing of the holdings StockBrain opened.

Each holding's target is its conviction weight's share of the whole account
(see :func:`stockbrain.risk.rules.conviction_targets`) -- the same arithmetic a
new buy is sized with, so a rebalance moves the book toward exactly what the
bot would have built from scratch today.

Rebalancing never trades directly. A trim is an ordinary REDUCE (or SELL)
proposal and a top-up an ordinary BUY proposal, each on the normal risk and
authorization path. Trims are proposed before top-ups because a top-up is
funded by the cash a trim frees; top-ups the cash cannot fund yet are left for
the next ``/rebalance`` once the sales have filled.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from stockbrain.broker.account_state import AccountStateService
from stockbrain.db.base import utcnow
from stockbrain.enums import RuleOutcome, ThesisAction
from stockbrain.logging import get_logger
from stockbrain.proposals.holdings import HoldingBelief, load_holding_beliefs
from stockbrain.proposals.service import ProposalService
from stockbrain.risk.exits import ExitSignal
from stockbrain.risk.models import RuleResult
from stockbrain.risk.rules import conviction_targets

__all__ = ["RebalanceLine", "RebalancePlan", "RebalanceService"]

log = get_logger(__name__)

#: A trim this close to the whole position is proposed as a full SELL.
_FULL_EXIT_FRACTION = Decimal("0.98")


@dataclass(frozen=True, slots=True)
class RebalanceLine:
    broker_ticker: str
    value: Decimal
    target: Decimal
    action: str
    """``TRIM``, ``TOP_UP``, ``HOLD``, ``REVIEW`` or ``SKIP``."""
    reason: str = ""
    thesis_id: uuid.UUID | None = None

    @property
    def delta(self) -> Decimal:
        return self.target - self.value


@dataclass(slots=True)
class RebalancePlan:
    currency: str | None
    total: Decimal
    lines: list[RebalanceLine] = field(default_factory=list)
    unavailable: str | None = None
    submitted: dict[str, str] = field(default_factory=dict)
    """After execution: ticker -> what happened."""


class RebalanceService:
    def __init__(
        self,
        proposals: ProposalService,
        account_state: AccountStateService,
        *,
        min_move: Decimal = Decimal("5"),
        max_thesis_age: dt.timedelta = dt.timedelta(days=3),
    ) -> None:
        self.proposals = proposals
        self.account_state = account_state
        self.database = proposals.database
        self.min_move = min_move
        self.max_thesis_age = max_thesis_age

    async def plan(self, *, now: dt.datetime | None = None) -> RebalancePlan:
        moment = now or utcnow()
        config = self.proposals.config
        account, reason = await self.account_state.load(
            max_age_seconds=config.max_account_state_age_seconds, now=moment
        )
        if account is None:
            return RebalancePlan(currency=None, total=Decimal(0), unavailable=reason)
        if config.sizing_mode != "conviction":
            return RebalancePlan(
                currency=account.currency,
                total=account.total_value,
                unavailable="rebalancing needs RISK_SIZING_MODE=conviction",
            )

        async with self.database.session() as session:
            beliefs = await load_holding_beliefs(
                session,
                broker=self.proposals.broker,
                environment=self.proposals.settings.t212_env.value,
            )
        weights = {belief.broker_ticker: belief.weight(config) for belief in beliefs}
        if config.target_positions:
            # Fixed-count mode: every holding's target is its own 1/N share.
            share = account.total_value / Decimal(config.target_positions)
            targets = {
                ticker: min(
                    share * weight,
                    account.total_value * config.max_position_pct_of_portfolio,
                )
                for ticker, weight in weights.items()
            }
        else:
            targets = conviction_targets(account.total_value, weights, config)

        plan = RebalancePlan(currency=account.currency, total=account.total_value)
        for belief in beliefs:
            position = account.position(belief.broker_ticker)
            value = (position.market_value if position else None) or Decimal(0)
            target = targets[belief.broker_ticker]
            plan.lines.append(self._line(belief, value, target, moment))
        plan.lines.sort(key=lambda line: line.delta)
        return plan

    def _line(
        self, belief: HoldingBelief, value: Decimal, target: Decimal, now: dt.datetime
    ) -> RebalanceLine:
        def line(action: str, reason: str = "") -> RebalanceLine:
            return RebalanceLine(
                broker_ticker=belief.broker_ticker,
                value=value,
                target=target,
                action=action,
                reason=reason,
                thesis_id=belief.thesis_id,
            )

        if not belief.managed:
            return line("SKIP", "not opened by StockBrain")
        if belief.action is not ThesisAction.BUY:
            # HOLD included: a successor HOLD is an exit signal on this system.
            said = belief.action.value if belief.action else "nothing"
            return line("SKIP", f"latest research says {said}; the exit path handles it")
        if belief.thesis_at is None or now - belief.thesis_at > self.max_thesis_age:
            return line("REVIEW", "research is stale; run /review first")
        delta = target - value
        if abs(delta) < self.min_move:
            return line("HOLD", "within the minimum move")
        return line("TOP_UP" if delta > 0 else "TRIM")

    async def execute(self, *, now: dt.datetime | None = None) -> RebalancePlan:
        plan = await self.plan(now=now)
        if plan.unavailable:
            return plan
        trims = [line for line in plan.lines if line.action == "TRIM"]
        top_ups = [line for line in plan.lines if line.action == "TOP_UP"]
        for line in trims:
            fraction = min(Decimal(1), -line.delta / line.value) if line.value > 0 else None
            if fraction is None or fraction <= 0:
                continue
            full = fraction >= _FULL_EXIT_FRACTION
            result = await self.proposals.generate_exit(
                line.broker_ticker,
                self._signal(
                    ThesisAction.SELL if full else ThesisAction.REDUCE,
                    f"rebalance: {line.value:.2f} held against a {line.target:.2f} target",
                    fraction=None if full else fraction,
                ),
                now=now,
            )
            plan.submitted[line.broker_ticker] = (
                f"trim proposed ({fraction:.0%})" if result.created else result.reason
            )
        for line in sorted(top_ups, key=lambda item: -item.delta):
            result = await self.proposals.generate_exit(
                line.broker_ticker,
                self._signal(
                    ThesisAction.BUY,
                    f"rebalance: {line.value:.2f} held against a {line.target:.2f} target",
                ),
                now=now,
                thesis_id=line.thesis_id,
            )
            plan.submitted[line.broker_ticker] = (
                "top-up proposed" if result.created else result.reason
            )
        log.info("rebalance_executed", **{k: v[:80] for k, v in plan.submitted.items()})
        return plan

    @staticmethod
    def _signal(
        action: ThesisAction, reason: str, *, fraction: Decimal | None = None
    ) -> ExitSignal:
        return ExitSignal(
            rule_id="rebalance",
            action=action,
            reason=reason,
            rule=RuleResult(
                rule_id="rebalance",
                rule_version=1,
                outcome=RuleOutcome.WARN,
                reason="operator-requested rebalance toward conviction targets",
                observed=None,
                threshold=None,
            ),
            fraction=fraction,
        )
