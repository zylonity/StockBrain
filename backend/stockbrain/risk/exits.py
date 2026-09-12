"""Deterministic exit rules for open positions.

Six rules, one of which fires, in a precedence that is declared rather than
emergent.  Ordering is by what is at stake per unit of delay: limiting a loss
first, protecting a gain second, acting on changed research third, banking a
target fourth, and recycling dead capital last.

Every rule but ``volatility_stop`` is a ratio against ``Position.average_price``,
so currency cancels and no FX conversion belongs here.  ``volatility_stop``
compares a price *difference* -- ``peak - price`` against ``k * ATR`` -- so
currency does not cancel; it trusts only an ATR whose period and currency match
the position, and the refresh asserts that currency at write time.  The prices
are broker-supplied and explicitly not real-time: a signal from this module is a
*trigger*, and the reference price on the resulting proposal always comes from
the market-data path through the ordinary evaluator.

Nothing here reads a database, a setting or a clock.  ``now`` is a parameter.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from stockbrain.enums import RuleOutcome, ThesisAction, TimeHorizon
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import RuleResult

__all__ = [
    "EXIT_PRECEDENCE",
    "ExitFloors",
    "ExitObservation",
    "ExitSignal",
    "evaluate_exit",
    "exit_floors",
    "roi_target_for",
]

ZERO = Decimal(0)

#: The order rules are consulted in.  The first to fire wins and the rest are
#: not evaluated, so two rules can never propose two orders for one position.
EXIT_PRECEDENCE: tuple[str, ...] = (
    "hard_stop",
    "volatility_stop",
    "trailing_stop",
    "thesis_superseded",
    "roi_target",
    "horizon_elapsed",
)


@dataclass(frozen=True, slots=True)
class ExitObservation:
    """One open position as the sweep found it."""

    broker_ticker: str
    quantity: Decimal
    quantity_available: Decimal
    average_price: Decimal
    current_price: Decimal
    peak_price: Decimal | None
    peak_observations: int
    opened_at: dt.datetime
    horizon: TimeHorizon
    thesis_superseded: bool
    atr: Decimal | None = None
    atr_as_of: dt.date | None = None

    @property
    def gain(self) -> Decimal:
        """Fractional result against average cost.  Positive is profit."""
        return (self.current_price - self.average_price) / self.average_price

    def minutes_held(self, now: dt.datetime) -> int:
        return max(0, int((now - self.opened_at).total_seconds() // 60))


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """Which rule fired, and what it proposes."""

    rule_id: str
    action: ThesisAction
    reason: str
    rule: RuleResult


@dataclass(frozen=True, slots=True)
class ExitFloors:
    """Where each exit rule would act for one open position.

    ``nearest_floor`` and ``nearest_rule`` answer "which price matters first?":
    the highest of the non-``None`` floors, so the operator sees the barrier the
    position would meet soonest.  A time -- the horizon -- is not a price and
    never competes here.
    """

    hard_stop: Decimal
    volatility_floor: Decimal | None
    trailing_floor: Decimal | None
    roi_target_price: Decimal | None
    horizon_ends_at: dt.datetime
    nearest_floor: Decimal
    nearest_rule: str


def roi_target_for(horizon: TimeHorizon, minutes_held: int, config: RiskConfig) -> Decimal:
    """The profit required to bank a reduction at this age.

    The applicable row is the last one whose minute threshold has been passed,
    so the table reads as a decay rather than a set of windows.
    """
    target = ZERO
    threshold = -1
    for row_horizon, row_minutes, row_target in config.exit_roi_decay:
        if row_horizon is not horizon:
            continue
        if row_minutes <= minutes_held and row_minutes > threshold:
            threshold, target = row_minutes, row_target
    return target


def _fired(rule_id: str, reason: str, observed: str, threshold: str) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_version=1,
        outcome=RuleOutcome.WARN,
        reason=reason,
        observed=observed,
        threshold=threshold,
    )


def _volatility_armed(
    observation: ExitObservation, config: RiskConfig, now: dt.datetime
) -> Decimal | None:
    """The Chandelier floor, or ``None`` when the volatility rule would skip.

    ``None`` when there is no peak, no usable ATR, a stale ATR, or a peak built
    from too few observations.  Shared with ``exit_floors`` so the floor shown
    to an operator is exactly the one the rule would act on.
    """
    peak = observation.peak_price
    atr = observation.atr
    atr_fresh = (
        observation.atr_as_of is not None
        and (now.date() - observation.atr_as_of).days <= config.exit_atr_max_age_days
    )
    if (
        peak is not None
        and atr is not None
        and atr > ZERO
        and atr_fresh
        and observation.peak_observations >= config.exit_min_peak_observations
    ):
        return peak - config.exit_atr_multiplier * atr
    return None


def _trailing_armed(observation: ExitObservation, config: RiskConfig) -> Decimal | None:
    """The trailing floor, or ``None`` until the position has genuinely run.

    ``None`` when there is no peak, the peak is built from too few observations,
    or the gain never reached the arm level.  Shared with ``exit_floors`` for
    the same reason ``_volatility_armed`` is.
    """
    peak = observation.peak_price
    if (
        peak is not None
        and observation.peak_observations >= config.exit_min_peak_observations
        and (peak - observation.average_price) / observation.average_price
        >= config.exit_trailing_arm_pct
    ):
        return peak * (Decimal(1) - config.exit_trailing_pct)
    return None


def evaluate_exit(
    observation: ExitObservation, config: RiskConfig, *, now: dt.datetime
) -> ExitSignal | None:
    """The first rule in ``EXIT_PRECEDENCE`` that fires, or ``None`` to hold.

    ``None`` covers "nothing to do" and "nothing can be done" alike: a position
    with no tradable shares or no usable cost basis produces no signal, because
    a proposal the broker would refuse is worse than no proposal.
    """
    if observation.average_price <= ZERO or observation.current_price <= ZERO:
        return None
    if observation.quantity_available <= ZERO:
        return None

    gain = observation.gain
    minutes = observation.minutes_held(now)

    # 1. hard_stop -- the floor, never widened.
    if gain < -config.exit_hard_stop_pct:
        return ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason=(
                f"the position is {_pct(gain)} against average cost, past the "
                f"{_pct(-config.exit_hard_stop_pct)} floor"
            ),
            rule=_fired(
                "hard_stop",
                "the hard stop was breached",
                _pct(gain),
                _pct(-config.exit_hard_stop_pct),
            ),
        )

    # 2. volatility_stop -- a Chandelier floor, `k` ATRs under the high-water
    #    mark.  Volatility-scaled where the flat rules are not: a quiet name is
    #    stopped tight, a wild one is given room.  ATR is research-grade data
    #    and may be missing or stale; then this rule skips and the flat stops
    #    stand.  Needs the same trustworthy peak the trailing rule needs.
    floor = _volatility_armed(observation, config, now)
    if floor is not None and observation.current_price < floor:
        return ExitSignal(
            rule_id="volatility_stop",
            action=ThesisAction.SELL,
            reason=(
                f"the price fell to {observation.current_price}, through the volatility "
                f"floor at {floor} ({config.exit_atr_multiplier} x ATR {observation.atr} "
                f"below the peak of {observation.peak_price})"
            ),
            rule=_fired(
                "volatility_stop",
                "the volatility floor was breached",
                str(observation.current_price),
                str(floor),
            ),
        )

    # 3. trailing_stop -- armed only once the position has genuinely run, and
    #    only against a peak built from enough observations to mean something.
    floor = _trailing_armed(observation, config)
    if floor is not None and observation.current_price < floor:
        return ExitSignal(
            rule_id="trailing_stop",
            action=ThesisAction.SELL,
            reason=(
                f"the price fell to {observation.current_price} from a peak of "
                f"{observation.peak_price}, through the {_pct(config.exit_trailing_pct)} "
                f"trailing floor at {floor}"
            ),
            rule=_fired(
                "trailing_stop",
                "the trailing floor was breached",
                str(observation.current_price),
                str(floor),
            ),
        )

    # 4. thesis_superseded -- research has published a newer conclusion about
    #    this company, so the reason recorded for holding is out of date.
    if observation.thesis_superseded:
        return ExitSignal(
            rule_id="thesis_superseded",
            action=ThesisAction.SELL,
            reason="a newer thesis supersedes the one this position was opened on",
            rule=_fired(
                "thesis_superseded",
                "the opening thesis has been superseded",
                "superseded",
                "the opening thesis is current",
            ),
        )

    target = roi_target_for(observation.horizon, minutes, config)

    # 5. roi_target -- bank half into strength.  A zero target belongs to
    #    horizon_elapsed, not here: "any profit at all" is a statement about
    #    the thesis expiring, not about a target being met.
    if target > ZERO and gain >= target:
        return ExitSignal(
            rule_id="roi_target",
            action=ThesisAction.REDUCE,
            reason=(
                f"the position is up {_pct(gain)} against the {_pct(target)} required "
                f"{minutes} minutes into a {observation.horizon.value} thesis"
            ),
            rule=_fired(
                "roi_target", "the decaying profit target was met", _pct(gain), _pct(target)
            ),
        )

    # 6. horizon_elapsed -- the thesis has run out of time and the position is
    #    closed whatever its result; a loss past the hard-stop floor never
    #    reaches this rule because hard_stop is evaluated first.
    if target == ZERO:
        return ExitSignal(
            rule_id="horizon_elapsed",
            action=ThesisAction.SELL,
            reason=(
                f"the {observation.horizon.value} thesis has been held {minutes} minutes, "
                f"past its horizon, at {_pct(gain)}"
            ),
            rule=_fired(
                "horizon_elapsed",
                "the thesis horizon elapsed",
                f"{minutes} minutes",
                "within the thesis horizon",
            ),
        )

    return None


def _horizon_ends_at(observation: ExitObservation, config: RiskConfig) -> dt.datetime:
    """The instant the thesis runs out of time.

    The horizon's terminal ROI row (``target == 0``) is the boundary the
    ``horizon_elapsed`` rule fires past; the smallest such ``minutes`` is used so
    a table with more than one terminal row still names one instant.
    """
    terminal_minutes = [
        row_minutes
        for row_horizon, row_minutes, row_target in config.exit_roi_decay
        if row_horizon is observation.horizon and row_target == ZERO
    ]
    if not terminal_minutes:
        raise ValueError(f"no terminal (target == 0) ROI row for {observation.horizon.value}")
    return observation.opened_at + dt.timedelta(minutes=min(terminal_minutes))


def exit_floors(
    observation: ExitObservation, config: RiskConfig, *, now: dt.datetime
) -> ExitFloors | None:
    """Every floor an exit rule would act on, computed from the same predicates.

    Read-only and deterministic: no broker, no database, no clock of its own.
    ``nearest_floor`` is the highest of the price floors -- the one the position
    would meet first -- and ``nearest_rule`` names the rule that owns it.  The
    horizon is a time, so it is reported but never competes for nearest.

    ``None`` when the observation is unusable in the same way ``evaluate_exit``
    refuses it: a non-positive cost basis or current price has no meaningful
    floors, and dividing by one would raise instead of answering.
    """
    if observation.average_price <= ZERO or observation.current_price <= ZERO:
        return None

    target = roi_target_for(observation.horizon, observation.minutes_held(now), config)
    hard_stop = observation.average_price * (Decimal(1) - config.exit_hard_stop_pct)
    volatility_floor = _volatility_armed(observation, config, now)
    trailing_floor = _trailing_armed(observation, config)
    roi_target_price = observation.average_price * (Decimal(1) + target) if target > ZERO else None

    nearest_rule = "hard_stop"
    nearest_floor = hard_stop
    for rule_id, floor in (
        ("volatility_stop", volatility_floor),
        ("trailing_stop", trailing_floor),
        ("roi_target", roi_target_price),
    ):
        if floor is not None and floor > nearest_floor:
            nearest_rule, nearest_floor = rule_id, floor

    return ExitFloors(
        hard_stop=hard_stop,
        volatility_floor=volatility_floor,
        trailing_floor=trailing_floor,
        roi_target_price=roi_target_price,
        horizon_ends_at=_horizon_ends_at(observation, config),
        nearest_floor=nearest_floor,
        nearest_rule=nearest_rule,
    )


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"
