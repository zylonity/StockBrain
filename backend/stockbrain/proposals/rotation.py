"""Research-first replacement of a weaker managed holding.

Rotation never swaps positions in one opaque operation.  A capacity-blocked,
high-confidence BUY may request fresh research on the weakest-confidence
holding.  Only after that review still trails the candidate is an ordinary
SELL proposal produced.  The BUY is retried only after broker reconciliation
shows the old holding has actually gone.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.enums import ResearchStatus, RuleOutcome, ThesisAction
from stockbrain.intelligence.research_service import ResearchService
from stockbrain.logging import get_logger
from stockbrain.proposals.service import STAGE_GENERATION, ProposalService
from stockbrain.proposals.state_machine import ACTIVE_STATUSES
from stockbrain.risk.exits import ExitSignal
from stockbrain.risk.models import RuleResult

log = get_logger(__name__)

_CAPACITY_RULES = {
    "max_aggregate_exposure",
    "active_proposal_exposure",
    "min_cash_reserve",
    "max_active_proposals",
}


class PortfolioRotationService:
    def __init__(
        self,
        proposals: ProposalService,
        research: ResearchService,
        *,
        candidate_floor: Decimal,
        minimum_advantage: Decimal,
    ) -> None:
        self.proposals = proposals
        self.research = research
        self.database = proposals.database
        self.candidate_floor = candidate_floor
        self.minimum_advantage = minimum_advantage

    async def sweep(self) -> dict[str, int]:
        counts = {"reviews_requested": 0, "sales_proposed": 0, "buys_retried": 0}
        counts["sales_proposed"] += await self._process_reviews()
        counts["buys_retried"] += await self._retry_after_sale()
        counts["reviews_requested"] += await self._request_one_review()
        if any(counts.values()):
            log.info("portfolio_rotation_sweep", **counts)
        return counts

    async def _request_one_review(self) -> int:
        async with self.database.session() as session:
            evaluations = list(
                await session.scalars(
                    sa.select(RiskEvaluation)
                    .join(Thesis, Thesis.id == RiskEvaluation.thesis_id)
                    .where(
                        RiskEvaluation.stage == STAGE_GENERATION,
                        RiskEvaluation.deferred.is_(False),
                        Thesis.action == ThesisAction.BUY,
                        Thesis.confidence >= float(self.candidate_floor),
                        ~sa.exists(
                            sa.select(TradeProposal.id).where(TradeProposal.thesis_id == Thesis.id)
                        ),
                    )
                    .order_by(Thesis.confidence.desc(), RiskEvaluation.created_at.desc())
                    .limit(50)
                )
            )
            candidate_eval = next(
                (
                    item
                    for item in evaluations
                    if _CAPACITY_RULES
                    & {
                        str(rule.get("rule_id"))
                        for rule in item.rules
                        if rule.get("outcome") == RuleOutcome.BLOCK.value
                    }
                ),
                None,
            )
            if candidate_eval is None or candidate_eval.thesis_id is None:
                return 0
            candidate = await session.get(Thesis, candidate_eval.thesis_id)
            if candidate is None:
                return 0
            already = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ResearchRun)
                .where(
                    ResearchRun.trigger == "POSITION_REASSESSMENT",
                    ResearchRun.rerun_id == candidate.id,
                )
            )
            if already:
                return 0

            weakest: tuple[Decimal, TradeProposal, ResearchRun] | None = None
            positions = list(
                await session.scalars(
                    sa.select(Position).where(
                        Position.broker == self.proposals.broker, Position.quantity > 0
                    )
                )
            )
            for position in positions:
                origin = await self.proposals.origin_proposal(session, position.broker_ticker)
                if origin is None or origin.thesis_id is None or origin.research_confidence is None:
                    continue
                origin_run = (
                    await session.get(ResearchRun, origin.research_run_id)
                    if origin.research_run_id
                    else None
                )
                if origin_run is None or origin_run.impact_id is None:
                    continue
                score = Decimal(str(origin.research_confidence))
                if weakest is None or score < weakest[0]:
                    weakest = (score, origin, origin_run)
            if (
                weakest is None
                or Decimal(str(candidate.confidence)) - weakest[0] < self.minimum_advantage
            ):
                return 0
            _, origin, origin_run = weakest
            impact_id = origin_run.impact_id
            origin_thesis_id = origin.thesis_id
            assert impact_id is not None and origin_thesis_id is not None

        async with self.database.transaction() as session:
            await self.research.request(
                session,
                impact_id,
                rerun_id=candidate.id,
                previous_thesis_id=origin_thesis_id,
            )
        return 1

    async def _process_reviews(self) -> int:
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    sa.select(ResearchRun, Thesis)
                    .join(Thesis, Thesis.research_run_id == ResearchRun.id)
                    .where(
                        ResearchRun.trigger == "POSITION_REASSESSMENT",
                        ResearchRun.rerun_id.is_not(None),
                        ResearchRun.status == ResearchStatus.SUCCEEDED,
                    )
                    .order_by(ResearchRun.completed_at.desc())
                    .limit(50)
                )
            ).all()
            for run, reviewed in rows:
                candidate = await session.get(Thesis, run.rerun_id)
                packet = run.research_packet or {}
                company = packet.get("company") or {}
                ticker = company.get("broker_ticker")
                if candidate is None or not ticker:
                    continue
                candidate_already_actionable = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(TradeProposal)
                    .where(TradeProposal.thesis_id == candidate.id)
                )
                if candidate_already_actionable:
                    continue
                active = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(TradeProposal)
                    .where(
                        TradeProposal.broker_ticker == ticker,
                        TradeProposal.status.in_(ACTIVE_STATUSES),
                    )
                )
                position = await session.scalar(
                    sa.select(Position).where(
                        Position.broker == self.proposals.broker,
                        Position.broker_ticker == ticker,
                        Position.quantity > 0,
                    )
                )
                advantage = Decimal(str(candidate.confidence)) - Decimal(str(reviewed.confidence))
                if not position or active or advantage < self.minimum_advantage:
                    continue
                signal = ExitSignal(
                    rule_id="portfolio_rotation",
                    action=ThesisAction.SELL,
                    reason=(
                        f"fresh research scored the holding {reviewed.confidence:.2f}, "
                        f"below replacement candidate {candidate.confidence:.2f}"
                    ),
                    rule=RuleResult(
                        rule_id="portfolio_rotation",
                        rule_version=1,
                        outcome=RuleOutcome.WARN,
                        reason="the reviewed holding trails a capacity-blocked replacement",
                        observed=str(reviewed.confidence),
                        threshold=str(Decimal(str(candidate.confidence)) - self.minimum_advantage),
                    ),
                )
                break
            else:
                return 0
        result = await self.proposals.generate_exit(ticker, signal)
        return int(result.created)

    async def _retry_after_sale(self) -> int:
        async with self.database.session() as session:
            rows = list(
                await session.scalars(
                    sa.select(ResearchRun)
                    .where(
                        ResearchRun.trigger == "POSITION_REASSESSMENT",
                        ResearchRun.rerun_id.is_not(None),
                        ResearchRun.status == ResearchStatus.SUCCEEDED,
                    )
                    .order_by(ResearchRun.completed_at.desc())
                    .limit(50)
                )
            )
            for run in rows:
                ticker = ((run.research_packet or {}).get("company") or {}).get("broker_ticker")
                if not ticker or run.rerun_id is None:
                    continue
                position = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Position)
                    .where(Position.broker_ticker == ticker, Position.quantity > 0)
                )
                proposal = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(TradeProposal)
                    .where(TradeProposal.thesis_id == run.rerun_id)
                )
                if not position and not proposal:
                    candidate_id = uuid.UUID(str(run.rerun_id))
                    break
            else:
                return 0
        result = await self.proposals.generate(candidate_id)
        return int(result.created)
