"""Broker account state: read it, persist it, and refuse to guess it.

The broker remains authoritative; ``portfolio_snapshots`` and ``positions`` are
a derived local mirror so that risk evaluation, the GUI and (later)
reconciliation read one consistent picture without hammering endpoints limited
to one request per second.

The governing rule of this module is **fail closed**.  If no snapshot exists, or
the newest one is older than the configured limit, or it came from the other
broker environment, :meth:`AccountStateService.load` returns ``None`` with a
reason.  It never falls back to the last known balance: sizing a real order
against a balance that is no longer true is precisely the failure that a
freshness rule exists to prevent, and "the only number available" has never
been a reason to use a number.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.broker.trading212_account import Trading212AccountClient
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position
from stockbrain.db.session import Database
from stockbrain.enums import Broker
from stockbrain.logging import get_logger
from stockbrain.risk.models import AccountState, PositionState

__all__ = ["DEFAULT_ACCOUNT_ID", "AccountStateService", "AccountStateUnavailableReason"]

log = get_logger(__name__)

#: Trading 212's summary carries a numeric account id; until a snapshot exists
#: the proposal tables use this placeholder, which matches the column default
#: shipped in the initial schema.
DEFAULT_ACCOUNT_ID = "default"


class AccountStateUnavailableReason:
    NEVER_SYNCED = "no broker account snapshot has ever been captured"
    STALE = "the newest broker account snapshot is older than the configured limit"
    WRONG_ENVIRONMENT = "the newest snapshot came from a different broker environment"
    INCOMPLETE = "the newest snapshot is missing a currency or a total value"


@dataclass(slots=True)
class AccountSyncResult:
    account_id: str
    currency: str
    positions: int
    captured_at: dt.datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "currency": self.currency,
            "positions": self.positions,
            "captured_at": self.captured_at.isoformat(),
        }


class AccountStateService:
    """Sync and read broker account state.

    ``client`` is optional so that a deployment without broker credentials still
    gets a working service that honestly reports "never synced" rather than a
    missing attribute at the point a proposal needs a balance.
    """

    def __init__(
        self,
        database: Database,
        client: Trading212AccountClient | None = None,
        *,
        broker: Broker = Broker.TRADING212,
        broker_environment: str = "demo",
    ) -> None:
        self._database = database
        self._client = client
        self._broker = broker
        self._environment = broker_environment

    @property
    def configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    async def sync(self) -> AccountSyncResult:
        """Fetch the account summary and open positions, and persist both.

        Two reads, in that order.  The snapshot's ``captured_at`` is stamped
        once, before either call, so the persisted freshness never claims to be
        newer than the oldest number it contains.
        """
        if self._client is None:
            raise RuntimeError("trading212 account client is not configured")

        captured_at = utcnow()
        summary = await self._client.fetch_account_summary()
        positions = await self._client.fetch_positions()
        account_id = str(summary.id)

        async with self._database.transaction() as session:
            session.add(
                PortfolioSnapshot(
                    broker=self._broker,
                    account_id=account_id,
                    captured_at=captured_at,
                    currency=summary.currency,
                    cash_available=summary.cash.available_to_trade,
                    cash_reserved=summary.cash.reserved_for_orders,
                    cash_in_pies=summary.cash.in_pies,
                    invested_value=summary.investments.current_value,
                    result_value=summary.investments.unrealized_profit_loss,
                    total_value=summary.total_value,
                    broker_environment=self._environment,
                    raw={
                        "cash": summary.cash.model_dump(mode="json"),
                        "investments": summary.investments.model_dump(mode="json"),
                    },
                )
            )

            seen: list[str] = []
            for position in positions:
                seen.append(position.broker_ticker)
                values = {
                    "broker": self._broker,
                    "account_id": account_id,
                    "broker_ticker": position.broker_ticker,
                    "quantity": position.quantity,
                    "quantity_available": position.quantity_available_for_trading,
                    "average_price": position.average_price_paid,
                    "current_price": position.current_price,
                    "ppl": position.wallet_impact.unrealized_profit_loss,
                    "currency": position.instrument.currency,
                    "initial_fill_date": position.created_at,
                    "last_synced_at": captured_at,
                    "updated_at": captured_at,
                    "raw": {
                        "instrument": position.instrument.model_dump(mode="json"),
                        "wallet_impact": position.wallet_impact.model_dump(mode="json"),
                        "quantity_in_pies": str(position.quantity_in_pies),
                    },
                }
                statement = pg_insert(Position).values(created_at=captured_at, **values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            Position.broker,
                            Position.account_id,
                            Position.broker_ticker,
                        ],
                        set_={key: statement.excluded[key] for key in values if key != "broker"},
                    )
                )

            # A position the broker no longer reports is closed. It is deleted
            # rather than zeroed: `positions` mirrors current broker state, and
            # the historical record lives in the snapshot and the audit log.
            delete = sa.delete(Position).where(
                Position.broker == self._broker,
                Position.account_id == account_id,
            )
            if seen:
                delete = delete.where(Position.broker_ticker.not_in(seen))
            await session.execute(delete)

        log.info(
            "broker_account_synced",
            account_id=account_id,
            currency=summary.currency,
            positions=len(positions),
            environment=self._environment,
        )
        return AccountSyncResult(
            account_id=account_id,
            currency=summary.currency,
            positions=len(positions),
            captured_at=captured_at,
        )

    # ------------------------------------------------------------------
    async def load(
        self, *, max_age_seconds: Decimal, now: dt.datetime | None = None
    ) -> tuple[AccountState | None, str | None]:
        """Return the current account state, or ``(None, reason)``.

        The reason is a sentence an operator can act on, and it is what the
        ``account_state_available`` risk rule reports when it blocks.
        """
        moment = now or utcnow()
        async with self._database.session() as session:
            snapshot = (
                await session.execute(
                    sa.select(PortfolioSnapshot)
                    .where(PortfolioSnapshot.broker == self._broker)
                    .order_by(PortfolioSnapshot.captured_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if snapshot is None:
                return None, AccountStateUnavailableReason.NEVER_SYNCED
            if (
                snapshot.broker_environment is not None
                and snapshot.broker_environment != self._environment
            ):
                return None, AccountStateUnavailableReason.WRONG_ENVIRONMENT
            if snapshot.currency is None or snapshot.total_value is None:
                return None, AccountStateUnavailableReason.INCOMPLETE

            age = (moment - snapshot.captured_at).total_seconds()
            if Decimal(str(max(0.0, age))) > max_age_seconds:
                return None, (
                    f"{AccountStateUnavailableReason.STALE} "
                    f"({age:.0f}s old, limit {max_age_seconds}s)"
                )

            account_id = snapshot.account_id or DEFAULT_ACCOUNT_ID
            rows = (
                await session.execute(
                    sa.select(Position).where(
                        Position.broker == self._broker,
                        Position.account_id == account_id,
                    )
                )
            ).scalars()
            positions = {
                row.broker_ticker: PositionState(
                    broker_ticker=row.broker_ticker,
                    quantity=row.quantity,
                    quantity_available=(
                        row.quantity_available
                        if row.quantity_available is not None
                        else row.quantity
                    ),
                    currency=row.currency,
                    average_price=row.average_price,
                    current_price=row.current_price,
                    market_value=_market_value(row),
                )
                for row in rows
            }

        return (
            AccountState(
                broker=self._broker,
                account_id=account_id,
                currency=snapshot.currency,
                cash_available=snapshot.cash_available or Decimal(0),
                cash_reserved=snapshot.cash_reserved or Decimal(0),
                cash_in_pies=snapshot.cash_in_pies or Decimal(0),
                invested_value=snapshot.invested_value or Decimal(0),
                total_value=snapshot.total_value,
                captured_at=snapshot.captured_at,
                positions=positions,
            ),
            None,
        )


def _market_value(row: Position) -> Decimal | None:
    """The position's value in the *account* currency.

    Preferred from the broker's own ``walletImpact.currentValue``, which is
    already in the account currency and already accounts for FX.  The
    quantity-times-price fallback is only correct when the instrument and the
    account share a currency, which the risk engine's currency rule enforces
    separately -- so it is used only when the broker did not supply a value.
    """
    wallet = (row.raw or {}).get("wallet_impact")
    if isinstance(wallet, dict):
        current = wallet.get("current_value")
        if current is not None:
            return Decimal(str(current))
    if row.current_price is not None:
        return row.quantity * row.current_price
    return None
