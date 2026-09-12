"""Centralised, versioned risk configuration.

Every threshold the deterministic engine consults lives here, in one frozen
object with one content-addressed version string.  Two properties follow from
that and both matter:

* **A proposal records the version it was evaluated under.**  "Why was this
  allowed?" is answerable months later even after the limits changed, and a
  proposal generated under superseded limits can be invalidated rather than
  quietly authorized against numbers nobody chose.
* **There is exactly one place to change a limit.**  A threshold rewritten at a
  call site is a threshold nobody can audit.

Values are :class:`~decimal.Decimal` throughout.  A binary float ratio applied
to a cash balance produces a cap that is *nearly* the configured one, and
"nearly" is not a property a risk limit may have.

The defaults are deliberately conservative and are **not** investment advice
(spec section 14); they are editable through the environment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stockbrain.enums import MarketSession, TimeHorizon

if TYPE_CHECKING:
    from stockbrain.config import Settings

__all__ = [
    "DEFAULT_ALLOWED_INSTRUMENT_TYPES",
    "DEFAULT_ALLOWED_SESSIONS",
    "RiskConfig",
    "SpreadPolicy",
    "risk_config_from_settings",
]

#: Instrument types StockBrain will price and size.  Trading 212's universe also
#: contains warrants, futures, forex and crypto lines; none of them is priced by
#: the Alpaca US-equity feed, so none may be sized.
DEFAULT_ALLOWED_INSTRUMENT_TYPES: tuple[str, ...] = ("STOCK", "ETF")

#: Sessions in which a market order may be sized.  Pre- and after-hours books
#: are thin enough that the spread ceiling would usually reject them anyway;
#: excluding them by name makes the intent explicit rather than incidental.
DEFAULT_ALLOWED_SESSIONS: tuple[MarketSession, ...] = (MarketSession.REGULAR,)


class SpreadPolicy:
    """What an excessively wide but otherwise healthy book does to a proposal.

    ``BLOCK`` is the default.  ``REDUCE`` exists because a wide book is a
    liquidity statement, not a correctness failure, and an operator may prefer
    a smaller position to none -- but it is an explicit choice, never a silent
    fallback.  Every other abnormal book shape (missing, one-sided, crossed,
    locked, non-positive) blocks under *both* policies: those are not "wide",
    they are "unusable".
    """

    BLOCK = "block"
    REDUCE = "reduce"

    ALL = (BLOCK, REDUCE)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Every deterministic limit, in one auditable object."""

    # -- Instrument and identity ---------------------------------------
    allowed_instrument_types: tuple[str, ...] = DEFAULT_ALLOWED_INSTRUMENT_TYPES
    allowed_sessions: tuple[MarketSession, ...] = DEFAULT_ALLOWED_SESSIONS
    require_known_session: bool = True
    """An UNKNOWN session means no schedule covered the instant.  Treating that
    as tradable would let a listing with unsynced metadata size at any hour."""

    # -- Market data ----------------------------------------------------
    max_quote_age_seconds: Decimal = Decimal("15")
    max_spread_bps: Decimal = Decimal("50")
    """0.50% of mid.  A liquid US equity trades far inside this in regular
    hours; the live overnight AAPL book measured ~1024 bps."""

    spread_policy: str = SpreadPolicy.BLOCK
    wide_spread_size_factor: Decimal = Decimal("0.5")
    """Only consulted under ``SpreadPolicy.REDUCE``."""

    # -- Account state ---------------------------------------------------
    max_account_state_age_seconds: Decimal = Decimal("300")
    require_same_currency: bool = True
    """No FX rate source is configured, so a cross-currency size would have to
    invent one.  v1 refuses instead (spec section 12's "unsupported")."""

    # -- Trade sizing ----------------------------------------------------
    max_notional_per_trade: Decimal = Decimal("500")
    max_trade_pct_of_portfolio: Decimal = Decimal("0.02")
    max_position_pct_of_portfolio: Decimal = Decimal("0.03")
    max_aggregate_exposure_pct: Decimal = Decimal("0.60")
    min_cash_reserve_pct: Decimal = Decimal("0.10")
    min_trade_notional: Decimal = Decimal("20")
    allow_fractional_quantity: bool = False
    """Trading 212 supports fractional shares but documents **no** minimum
    quantity and no step, so nothing here may invent one.  Whole shares rounded
    *down* are valid for every instrument and can never exceed a cap."""

    # -- Proposal population ---------------------------------------------
    max_active_proposals: int = 5
    max_active_proposal_exposure_pct: Decimal = Decimal("0.10")
    proposal_ttl_minutes: int = 30
    max_reference_price_drift_pct: Decimal = Decimal("0.01")
    """How far the market may move from the reference price before the sizing
    stops describing the trade the proposal says it is."""

    # -- Research confidence ----------------------------------------------
    min_research_confidence: Decimal = Decimal("0.70")
    confidence_modulates_size: bool = True
    min_confidence_size_factor: Decimal = Decimal("0.5")
    """The smallest fraction confidence may scale a size to.  Confidence can
    only ever *shrink* a position inside the hard caps, and can never lift a
    BLOCK: it is a ranking feature, not a calibrated probability."""

    # -- Action semantics --------------------------------------------------
    reduce_fraction: Decimal = Decimal("0.5")
    """REDUCE is a deterministic partial exit, not a liquidation."""

    allow_short_selling: bool = False

    # -- Exit policy -------------------------------------------------------
    exit_hard_stop_pct: Decimal = Decimal("0.08")
    """Loss from average cost at which the whole position is proposed for exit.
    A floor, never widened: the one number in this config whose job is to be
    hit."""

    exit_trailing_pct: Decimal = Decimal("0.05")
    """How far below the high-water mark the trailing floor sits, once armed."""

    exit_trailing_arm_pct: Decimal = Decimal("0.10")
    """Gain from average cost at which trailing switches on.  Below this the
    hard stop is the only floor, so an ordinary wobble after entry does not
    close a position that never went anywhere."""

    exit_min_peak_observations: int = 3
    """Syncs a peak must be built from before the trailing rule trusts it.  One
    observation is an entry price wearing a peak's name."""

    exit_roi_decay: tuple[tuple[TimeHorizon, int, Decimal], ...] = (
        (TimeHorizon.INTRADAY, 0, Decimal("0.04")),
        (TimeHorizon.INTRADAY, 240, Decimal("0.02")),
        (TimeHorizon.INTRADAY, 480, Decimal("0")),
        (TimeHorizon.DAYS, 0, Decimal("0.06")),
        (TimeHorizon.DAYS, 1440, Decimal("0.03")),
        (TimeHorizon.DAYS, 4320, Decimal("0")),
        (TimeHorizon.WEEKS, 0, Decimal("0.15")),
        (TimeHorizon.WEEKS, 10080, Decimal("0.08")),
        (TimeHorizon.WEEKS, 30240, Decimal("0")),
        (TimeHorizon.MONTHS, 0, Decimal("0.30")),
        (TimeHorizon.MONTHS, 43200, Decimal("0.15")),
        (TimeHorizon.MONTHS, 129600, Decimal("0")),
    )
    """Profit required to bank a reduction, by thesis horizon and minutes held.

    Demand a lot early, accept less as the thesis ages.  The terminal ``0`` row
    is the horizon-elapsed boundary: past it the position is closed whatever its
    result, because an event thesis that has not resolved by then is no longer
    the reason the position is held.
    """

    _version: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.spread_policy not in SpreadPolicy.ALL:
            raise ValueError(f"spread_policy must be one of {SpreadPolicy.ALL}")
        object.__setattr__(self, "_version", _digest(self.as_dict()))

    @property
    def version(self) -> str:
        """Content hash of every threshold.  Persisted with each proposal."""
        return self._version

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready view.  Decimals become strings so the digest is exact."""
        payload = asdict(self)
        payload.pop("_version", None)
        return {key: _jsonable(value) for key, value in sorted(payload.items())}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Canonicalised: ``300`` and ``300.0`` are the same limit, so they must
        # produce the same version. Plain ``str`` keeps trailing zeros, which
        # would make a config built from a float setting hash differently from
        # the identical literal. ``normalize`` alone yields ``3E+2``; the ``f``
        # format brings it back to ordinary notation.
        return format(value.normalize(), "f")
    if isinstance(value, MarketSession):
        return value.value
    if isinstance(value, TimeHorizon):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def risk_config_from_settings(settings: Settings) -> RiskConfig:
    """Build the process-wide risk configuration from typed settings.

    The quote-age limit is deliberately shared with the market-data subsystem's
    own setting rather than duplicated: two numbers that must agree are one
    number that will eventually disagree.
    """
    return RiskConfig(
        allowed_instrument_types=tuple(settings.risk_allowed_instrument_types)
        or DEFAULT_ALLOWED_INSTRUMENT_TYPES,
        allowed_sessions=tuple(MarketSession(name) for name in settings.risk_allowed_sessions)
        or DEFAULT_ALLOWED_SESSIONS,
        require_known_session=settings.risk_require_known_session,
        max_quote_age_seconds=Decimal(str(settings.market_data_max_quote_age_seconds)),
        max_spread_bps=settings.risk_max_spread_bps,
        spread_policy=settings.risk_spread_policy,
        wide_spread_size_factor=settings.risk_wide_spread_size_factor,
        max_account_state_age_seconds=Decimal(str(settings.risk_max_account_state_age_seconds)),
        require_same_currency=settings.risk_require_same_currency,
        max_notional_per_trade=settings.risk_max_notional_per_trade,
        max_trade_pct_of_portfolio=settings.risk_max_trade_pct,
        max_position_pct_of_portfolio=settings.risk_max_position_pct,
        max_aggregate_exposure_pct=settings.risk_max_aggregate_exposure_pct,
        min_cash_reserve_pct=settings.risk_min_cash_reserve_pct,
        min_trade_notional=settings.risk_min_trade_notional,
        allow_fractional_quantity=settings.risk_allow_fractional_quantity,
        max_active_proposals=settings.risk_max_active_proposals,
        max_active_proposal_exposure_pct=settings.risk_max_active_proposal_exposure_pct,
        proposal_ttl_minutes=settings.risk_proposal_ttl_minutes,
        max_reference_price_drift_pct=settings.risk_max_reference_price_drift_pct,
        min_research_confidence=settings.risk_min_research_confidence,
        confidence_modulates_size=settings.risk_confidence_modulates_size,
        min_confidence_size_factor=settings.risk_min_confidence_size_factor,
        reduce_fraction=settings.risk_reduce_fraction,
        exit_hard_stop_pct=settings.risk_exit_hard_stop_pct,
        exit_trailing_pct=settings.risk_exit_trailing_pct,
        exit_trailing_arm_pct=settings.risk_exit_trailing_arm_pct,
        exit_min_peak_observations=settings.risk_exit_min_peak_observations,
        allow_short_selling=False,
    )
