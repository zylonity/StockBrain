"""Deterministic position sizing.

Ordinary arithmetic on a fresh broker snapshot.  Nothing here reads a model
output other than two scalars the caller already validated -- an action and a
confidence factor bounded by the risk configuration -- and neither can raise a
size above what the caps permit.

Three choices are worth their reasons:

* **The reference price is the marketable side, not the mid.**  A market buy
  lifts the ask and a market sell hits the bid.  Sizing a buy against the mid
  systematically commits more cash than the cap allows, by half the spread,
  every time.  The safe direction is the expensive one.
* **Quantities round *down*.**  Trading 212 supports fractional shares but
  documents no minimum quantity and no step size -- Phase 4 measured
  ``minTradeQuantity`` populated on 0 of 17,452 live instruments -- so nothing
  here may invent one.  Whole shares rounded down are valid for every
  instrument and can never exceed a ceiling.  Fractional sizing is available
  but off by default.
* **A size that rounds to nothing is not a trade.**  It is reported as
  non-executable with the reason, rather than as a zero-quantity order.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from stockbrain.enums import OrderSide, ThesisAction
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import ZERO, AccountState, InstrumentIdentity, QuoteSnapshot
from stockbrain.risk.models import SizingResult as SizingResult

__all__ = ["ACTION_SIDES", "size_trade"]

#: How a research action becomes a broker side.  ``REDUCE`` and ``SELL`` differ
#: in quantity, not in direction; ``HOLD`` and ``NO_ACTION`` have no side at all
#: and must never produce an executable proposal.
ACTION_SIDES: dict[ThesisAction, OrderSide | None] = {
    ThesisAction.BUY: OrderSide.BUY,
    ThesisAction.SELL: OrderSide.SELL,
    ThesisAction.REDUCE: OrderSide.SELL,
    ThesisAction.HOLD: None,
    ThesisAction.NO_ACTION: None,
}

_QUANTITY_EXPONENT = Decimal("0.00000001")


def size_trade(
    *,
    action: ThesisAction,
    config: RiskConfig,
    identity: InstrumentIdentity,
    quote: QuoteSnapshot | None,
    account: AccountState | None,
    max_notional: Decimal,
    size_factor: Decimal = Decimal(1),
) -> SizingResult:
    """Produce the order parameters, or explain why there are none.

    ``max_notional`` is the minimum of every applicable cap, already computed by
    the engine.  ``size_factor`` is the product of every reduction factor
    (research confidence, a wide-spread policy) and is always in ``(0, 1]``.
    """
    side = ACTION_SIDES[action]
    reasons: list[str] = []
    currency = identity.currency

    if side is None:
        return SizingResult(
            side=None,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=ZERO,
            max_notional=ZERO,
            reference_price=None,
            currency=currency,
            reasons=(f"{action.value} produces no executable order",),
            executable=False,
        )

    if quote is None or quote.bid is None or quote.ask is None:
        return SizingResult(
            side=side,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=ZERO,
            max_notional=max_notional,
            reference_price=None,
            currency=currency,
            reasons=("no two-sided quote, so there is no reference price",),
            executable=False,
        )

    reference_price = quote.ask if side is OrderSide.BUY else quote.bid
    if reference_price <= ZERO:
        return SizingResult(
            side=side,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=ZERO,
            max_notional=max_notional,
            reference_price=None,
            currency=currency,
            reasons=("the marketable side of the quote carries no positive price",),
            executable=False,
        )
    reasons.append(
        f"reference price {reference_price} is the "
        f"{'ask' if side is OrderSide.BUY else 'bid'}, the side a market "
        f"{side.value.lower()} actually trades against"
    )

    if side is OrderSide.BUY:
        return _size_buy(
            config=config,
            identity=identity,
            reference_price=reference_price,
            currency=currency,
            max_notional=max_notional,
            size_factor=size_factor,
            reasons=reasons,
        )
    return _size_sell(
        action=action,
        config=config,
        identity=identity,
        account=account,
        reference_price=reference_price,
        currency=currency,
        reasons=reasons,
    )


def _size_buy(
    *,
    config: RiskConfig,
    identity: InstrumentIdentity,
    reference_price: Decimal,
    currency: str | None,
    max_notional: Decimal,
    size_factor: Decimal,
    reasons: list[str],
) -> SizingResult:
    max_quantity = _round_quantity(max_notional / reference_price, config)
    if max_notional <= ZERO:
        reasons.append("no notional headroom remains for an exposure-increasing trade")
        return SizingResult(
            side=OrderSide.BUY,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=ZERO,
            max_notional=ZERO,
            reference_price=reference_price,
            currency=currency,
            reasons=tuple(reasons),
            executable=False,
        )

    target_notional = max_notional * size_factor
    if size_factor < Decimal(1):
        reasons.append(f"size factor {size_factor} reduces {max_notional} to {target_notional}")

    quantity = _round_quantity(target_notional / reference_price, config)
    if quantity > max_quantity:  # pragma: no cover - defensive; factor is <= 1
        quantity = max_quantity
    actual_notional = quantity * reference_price

    if quantity <= ZERO:
        reasons.append(
            f"a target of {target_notional} {currency or ''} buys less than one "
            f"{'unit' if config.allow_fractional_quantity else 'whole share'} "
            f"at {reference_price}"
        )
        return SizingResult(
            side=OrderSide.BUY,
            quantity=ZERO,
            target_notional=target_notional,
            max_quantity=max_quantity,
            max_notional=max_notional,
            reference_price=reference_price,
            currency=currency,
            reasons=tuple(reasons),
            executable=False,
        )

    if actual_notional < config.min_trade_notional:
        reasons.append(
            f"{quantity} share(s) at {reference_price} is {actual_notional}, below the "
            f"{config.min_trade_notional} minimum trade notional"
        )
        return SizingResult(
            side=OrderSide.BUY,
            quantity=ZERO,
            target_notional=actual_notional,
            max_quantity=max_quantity,
            max_notional=max_notional,
            reference_price=reference_price,
            currency=currency,
            reasons=tuple(reasons),
            executable=False,
        )

    reasons.append(
        f"{quantity} share(s) at {reference_price} commits {actual_notional} "
        f"of the {max_notional} permitted"
    )
    if identity.max_open_quantity is not None:
        reasons.append(f"the broker caps open quantity at {identity.max_open_quantity}")
    return SizingResult(
        side=OrderSide.BUY,
        quantity=quantity,
        target_notional=actual_notional,
        max_quantity=max_quantity,
        max_notional=max_notional,
        reference_price=reference_price,
        currency=currency,
        reasons=tuple(reasons),
        executable=True,
    )


def _size_sell(
    *,
    action: ThesisAction,
    config: RiskConfig,
    identity: InstrumentIdentity,
    account: AccountState | None,
    reference_price: Decimal,
    currency: str | None,
    reasons: list[str],
) -> SizingResult:
    """Reduce or close an owned long.

    Exposure caps are deliberately not applied: they bound risk taken, not risk
    removed, and a cap that can stop a position from being closed is a hazard.
    Short selling is not enabled, so the available quantity is a hard ceiling
    and the result is never larger than the holding.
    """
    position = account.position(identity.broker_ticker) if account else None
    available = position.quantity_available if position else ZERO
    if available <= ZERO:
        reasons.append(
            f"{action.value} needs an existing long position; none is available for trading"
        )
        return SizingResult(
            side=OrderSide.SELL,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=ZERO,
            max_notional=ZERO,
            reference_price=reference_price,
            currency=currency,
            reasons=tuple(reasons),
            executable=False,
        )

    if action is ThesisAction.SELL:
        quantity = _round_quantity(available, config)
        reasons.append(f"SELL closes the whole available position of {available} share(s)")
    else:
        quantity = _round_quantity(available * config.reduce_fraction, config)
        reasons.append(
            f"REDUCE trims {config.reduce_fraction} of the {available} available share(s) -- "
            "a deterministic partial exit, not a liquidation"
        )
        if quantity <= ZERO and available >= Decimal(1) and not config.allow_fractional_quantity:
            # A two-share holding at a 0.5 fraction rounds to one share, but a
            # one-share holding rounds to zero. Selling the single share would
            # be a full exit wearing a partial exit's name, so it is refused and
            # the operator is told to use SELL.
            reasons.append(
                f"{available} share(s) cannot be partially reduced in whole shares; "
                "a full exit requires a SELL thesis"
            )

    max_quantity = _round_quantity(available, config)
    if quantity <= ZERO:
        return SizingResult(
            side=OrderSide.SELL,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=max_quantity,
            max_notional=max_quantity * reference_price,
            reference_price=reference_price,
            currency=currency,
            reasons=tuple(reasons),
            executable=False,
        )

    notional = quantity * reference_price
    return SizingResult(
        side=OrderSide.SELL,
        quantity=quantity,
        target_notional=notional,
        max_quantity=max_quantity,
        max_notional=max_quantity * reference_price,
        reference_price=reference_price,
        currency=currency,
        reasons=tuple(reasons),
        executable=True,
    )


def _round_quantity(raw: Decimal, config: RiskConfig) -> Decimal:
    """Round a raw quantity **down** to a quantity the broker will accept.

    Always down.  Rounding up can breach a cap by up to one share, and a cap
    that is breached "only a little" is not a cap.
    """
    if raw <= ZERO:
        return ZERO
    if config.allow_fractional_quantity:
        return raw.quantize(_QUANTITY_EXPONENT, rounding=ROUND_DOWN)
    return raw.quantize(Decimal(1), rounding=ROUND_DOWN)
