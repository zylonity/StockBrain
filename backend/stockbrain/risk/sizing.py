"""Deterministic position sizing.

Ordinary arithmetic on a fresh broker snapshot.  Nothing here reads a model
output other than two scalars the caller already validated -- an action and a
confidence factor bounded by the risk configuration -- and neither can raise a
size above what the caps permit.

Four choices are worth their reasons:

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
* **The caps move to the price, never the price to the caps.**  Every ceiling
  arrives denominated in the *account* currency and every price is denominated
  in the *instrument's*.  Converting the ceiling into the instrument's currency
  and dividing there is the arithmetic that is actually defined; dividing a GBP
  ceiling by a USD ask is the operation Phase 6 refused to perform, and it is
  what the FX snapshot exists to replace.  A snapshot that may not convert
  raises, and every caller reaches this function only after ``fx_available``
  and ``fx_freshness`` have passed -- but the raise is caught here and reported
  as a non-executable size, because a sizing function that can throw is a
  sizing function that can take down a sweep.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from stockbrain.enums import OrderSide, ThesisAction
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import (
    ZERO,
    AccountState,
    FxSnapshot,
    InstrumentIdentity,
    QuoteSnapshot,
)
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


def size_trade(
    *,
    action: ThesisAction,
    config: RiskConfig,
    identity: InstrumentIdentity,
    quote: QuoteSnapshot | None,
    account: AccountState | None,
    max_notional: Decimal,
    size_factor: Decimal = Decimal(1),
    fx: FxSnapshot | None = None,
) -> SizingResult:
    """Produce the order parameters, or explain why there are none.

    ``max_notional`` is the minimum of every applicable cap, already computed by
    the engine, and is denominated in the **account** currency.  ``size_factor``
    is the product of every reduction factor (research confidence, a
    wide-spread policy) and is always in ``(0, 1]``.  ``fx`` carries the
    conversion between the account and instrument currencies; ``None`` is
    treated as "same currency", which is only ever reached when the two codes
    already agree.
    """
    side = ACTION_SIDES[action]
    reasons: list[str] = []
    currency = identity.currency
    account_currency = account.currency if account else None
    snapshot = fx or FxSnapshot.same_currency_snapshot(currency or account_currency or "")

    if side is None:
        return _nothing(
            side=None,
            currency=currency,
            account_currency=account_currency,
            reasons=(f"{action.value} produces no executable order",),
        )

    if quote is None or quote.bid is None or quote.ask is None:
        return _nothing(
            side=side,
            currency=currency,
            account_currency=account_currency,
            max_notional=max_notional,
            reasons=("no two-sided quote, so there is no reference price",),
        )

    reference_price = quote.ask if side is OrderSide.BUY else quote.bid
    if reference_price <= ZERO:
        return _nothing(
            side=side,
            currency=currency,
            account_currency=account_currency,
            max_notional=max_notional,
            reasons=("the marketable side of the quote carries no positive price",),
        )
    reasons.append(
        f"reference price {reference_price} is the "
        f"{'ask' if side is OrderSide.BUY else 'bid'}, the side a market "
        f"{side.value.lower()} actually trades against"
    )

    if snapshot.conversion_required:
        if not snapshot.usable:
            # Reached only if a caller bypassed the gates. Reported rather than
            # raised, and carrying no size.
            return _nothing(
                side=side,
                currency=currency,
                account_currency=account_currency,
                max_notional=max_notional,
                reference_price=reference_price,
                reasons=("the FX snapshot may not convert: " + "; ".join(snapshot.blockers),),
            )
        reasons.append(
            f"caps are denominated in {snapshot.account_currency} and the price in "
            f"{snapshot.instrument_currency}; converted at {snapshot.pair}="
            f"{snapshot.rate} from {snapshot.provider} "
            f"({snapshot.direction().value.lower()})"
        )

    try:
        if side is OrderSide.BUY:
            return _size_buy(
                config=config,
                identity=identity,
                reference_price=reference_price,
                currency=currency,
                account_currency=account_currency,
                fx=snapshot,
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
            account_currency=account_currency,
            fx=snapshot,
            reasons=reasons,
        )
    except ValueError as exc:  # pragma: no cover - the gates run first
        reasons.append(f"the conversion could not be performed: {exc}")
        return _nothing(
            side=side,
            currency=currency,
            account_currency=account_currency,
            max_notional=max_notional,
            reference_price=reference_price,
            reasons=tuple(reasons),
        )


def _nothing(
    *,
    side: OrderSide | None,
    currency: str | None,
    account_currency: str | None,
    reasons: tuple[str, ...],
    max_notional: Decimal = ZERO,
    reference_price: Decimal | None = None,
) -> SizingResult:
    """A non-executable result.  Carries no quantity at all, never a zero order."""
    return SizingResult(
        side=side,
        quantity=ZERO,
        target_notional=ZERO,
        max_quantity=ZERO,
        max_notional=max_notional,
        reference_price=reference_price,
        currency=currency,
        account_currency=account_currency,
        notional_account_currency=ZERO,
        max_notional_instrument_currency=ZERO,
        reasons=reasons,
        executable=False,
    )


def _size_buy(
    *,
    config: RiskConfig,
    identity: InstrumentIdentity,
    reference_price: Decimal,
    currency: str | None,
    account_currency: str | None,
    fx: FxSnapshot,
    max_notional: Decimal,
    size_factor: Decimal,
    reasons: list[str],
) -> SizingResult:
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
            account_currency=account_currency,
            notional_account_currency=ZERO,
            max_notional_instrument_currency=ZERO,
            reasons=tuple(reasons),
            executable=False,
        )

    # The cap, moved into the currency the price is quoted in. Every quantity
    # below is derived from this number, so the conversion happens exactly once.
    max_notional_instrument = fx.to_instrument_currency(max_notional)
    max_quantity = _round_quantity(max_notional_instrument / reference_price, config, identity)

    target_notional_instrument = max_notional_instrument * size_factor
    if size_factor < Decimal(1):
        reasons.append(
            f"size factor {size_factor} reduces {max_notional_instrument} to "
            f"{target_notional_instrument} {currency or ''}".rstrip()
        )

    quantity = _round_quantity(target_notional_instrument / reference_price, config, identity)
    if quantity > max_quantity:  # pragma: no cover - defensive; factor is <= 1
        quantity = max_quantity
    actual_notional = quantity * reference_price
    actual_notional_account = fx.to_account_currency(actual_notional)

    if quantity <= ZERO:
        reasons.append(
            f"a target of {target_notional_instrument} {currency or ''} buys less than one "
            f"{'unit' if config.allow_fractional_quantity else 'whole share'} "
            f"at {reference_price}"
        )
        return SizingResult(
            side=OrderSide.BUY,
            quantity=ZERO,
            target_notional=target_notional_instrument,
            max_quantity=max_quantity,
            max_notional=max_notional,
            reference_price=reference_price,
            currency=currency,
            account_currency=account_currency,
            notional_account_currency=fx.to_account_currency(target_notional_instrument),
            max_notional_instrument_currency=max_notional_instrument,
            reasons=tuple(reasons),
            executable=False,
        )

    # The minimum trade notional is an account-currency limit, like every other
    # RISK_* money value, so it is compared against the converted number rather
    # than against the instrument-currency one.
    if actual_notional_account < config.min_trade_notional:
        reasons.append(
            f"{quantity} share(s) at {reference_price} is {actual_notional_account} "
            f"{account_currency or ''}, below the {config.min_trade_notional} minimum "
            f"trade notional".replace("  ", " ")
        )
        return SizingResult(
            side=OrderSide.BUY,
            quantity=ZERO,
            target_notional=actual_notional,
            max_quantity=max_quantity,
            max_notional=max_notional,
            reference_price=reference_price,
            currency=currency,
            account_currency=account_currency,
            notional_account_currency=actual_notional_account,
            max_notional_instrument_currency=max_notional_instrument,
            reasons=tuple(reasons),
            executable=False,
        )

    reasons.append(
        f"{quantity} share(s) at {reference_price} commits {actual_notional} "
        f"of the {max_notional_instrument} permitted"
    )
    if fx.conversion_required:
        reasons.append(
            f"that is {actual_notional_account} {account_currency or ''} against the "
            f"{max_notional} {account_currency or ''} cap".replace("  ", " ")
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
        account_currency=account_currency,
        notional_account_currency=actual_notional_account,
        max_notional_instrument_currency=max_notional_instrument,
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
    account_currency: str | None,
    fx: FxSnapshot,
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
        return _nothing(
            side=OrderSide.SELL,
            currency=currency,
            account_currency=account_currency,
            reference_price=reference_price,
            reasons=tuple(reasons),
        )

    if action is ThesisAction.SELL:
        quantity = _round_quantity(available, config, identity)
        reasons.append(f"SELL closes the whole available position of {available} share(s)")
    else:
        quantity = _round_quantity(available * config.reduce_fraction, config, identity)
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

    max_quantity = _round_quantity(available, config, identity)
    max_notional_instrument = max_quantity * reference_price
    max_notional_account = fx.to_account_currency(max_notional_instrument)
    if quantity <= ZERO:
        return SizingResult(
            side=OrderSide.SELL,
            quantity=ZERO,
            target_notional=ZERO,
            max_quantity=max_quantity,
            max_notional=max_notional_account,
            reference_price=reference_price,
            currency=currency,
            account_currency=account_currency,
            notional_account_currency=ZERO,
            max_notional_instrument_currency=max_notional_instrument,
            reasons=tuple(reasons),
            executable=False,
        )

    notional = quantity * reference_price
    return SizingResult(
        side=OrderSide.SELL,
        quantity=quantity,
        target_notional=notional,
        max_quantity=max_quantity,
        max_notional=max_notional_account,
        reference_price=reference_price,
        currency=currency,
        account_currency=account_currency,
        notional_account_currency=fx.to_account_currency(notional),
        max_notional_instrument_currency=max_notional_instrument,
        reasons=tuple(reasons),
        executable=True,
    )


def _round_quantity(raw: Decimal, config: RiskConfig, identity: InstrumentIdentity) -> Decimal:
    """Round a raw quantity **down** to a quantity the broker will accept.

    Always down.  Rounding up can breach a cap by up to one share, and a cap
    that is breached "only a little" is not a cap.

    The broker's precision is per instrument and published nowhere, so the
    identity's learned value is used when known and the configured default only
    until then.  ``Decimal(1).scaleb(-N)`` builds ``10 ** -N`` for every N
    including zero, where the step is a whole share.
    """
    if raw <= ZERO:
        return ZERO
    if not config.allow_fractional_quantity:
        return raw.quantize(Decimal(1), rounding=ROUND_DOWN)
    precision = identity.quantity_precision
    if precision is None:
        precision = config.default_quantity_precision
    return raw.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
