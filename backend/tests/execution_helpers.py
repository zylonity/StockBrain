"""Doubles and world builders for the broker execution surface.

Destructive and error-path testing must never be done by making a real broker
fail, so the seam is the five-operation
:class:`~stockbrain.execution.base.BrokerExecutionProvider`.  :class:`FakeProvider`
implements it and can be told to answer, to refuse, to time out, to report a
full pending-order queue, or to accept an order and then be unreadable -- every
shape the real client can produce.

:func:`order_view` builds a canonical broker order the way the Trading 212
adapter would, so reconciliation tests exercise the same matching rules the real
adapter feeds.  :func:`build` and :func:`authorized` assemble a funded, resolved,
authorized world; they live here rather than in one test module so a second
module does not have to import a test file to get at them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.session import Database
from stockbrain.enums import AuthorizationSource, Broker, ThesisAction
from stockbrain.execution.models import (
    BrokerAcknowledgement,
    BrokerOrderView,
    CandidateSearch,
    ExecutionCommand,
    PendingOrderCount,
)
from stockbrain.execution.service import ExecutionService
from stockbrain.proposals.service import ProposalService
from tests import proposal_helpers as helpers

__all__ = [
    "WEB_ACTOR",
    "FakeProvider",
    "acknowledgement_for",
    "authorized",
    "build",
    "execution_settings",
    "order_view",
]


def order_view(
    *,
    broker_order_id: str = "500100",
    broker_ticker: str = "AAPL_US_EQ",
    signed_quantity: Decimal | None = Decimal("2"),
    status: str = "NEW",
    order_type: str = "MARKET",
    initiated_from: str = "API",
    created_at: dt.datetime | None = None,
    filled_quantity: Decimal | None = None,
    filled_value: Decimal | None = None,
    side: str = "BUY",
    currency: str = "USD",
) -> BrokerOrderView:
    return BrokerOrderView(
        broker_order_id=broker_order_id,
        broker_ticker=broker_ticker,
        signed_quantity=signed_quantity,
        filled_quantity=filled_quantity,
        filled_value=filled_value,
        status=status,
        order_type=order_type,
        side=side,
        currency=currency,
        created_at=created_at or utcnow(),
        initiated_from=initiated_from,
        raw={"id": int(broker_order_id), "status": status},
    )


@dataclass
class FakeProvider:
    """A broker that does exactly what a test tells it to.

    ``submitted`` is the count that matters in nearly every test here: the
    invariant under examination is almost always "how many times did we
    transmit", and the answer must be at most one.
    """

    broker: Broker = Broker.TRADING212
    _environment: str = "demo"

    #: Raised from ``submit`` if set, instead of answering.
    error: Exception | None = None
    #: The acknowledgement ``submit`` returns when it does answer.
    acknowledgement: BrokerAcknowledgement | None = None
    #: ``False`` makes the local rate limiter deny -- a pre-send condition.
    slot_available: bool = True

    #: What ``find_candidates`` reports.
    search: CandidateSearch = field(default_factory=CandidateSearch)
    #: What ``fetch_order`` returns, keyed by broker order id.
    orders: dict[str, BrokerOrderView] = field(default_factory=dict)
    fetch_error: Exception | None = None

    #: What ``count_pending`` reports for any ticker. Zero pending and a
    #: successful read is the ordinary world; a test that cares sets these.
    pending_orders: int = 0
    pending_read_ok: bool = True
    pending_api_initiated: int = 0

    submitted: int = 0
    commands: list[ExecutionCommand] = field(default_factory=list)
    slot_requests: int = 0
    searches: int = 0
    pending_counts: int = 0

    @property
    def environment(self) -> str:
        return self._environment

    def set_environment(self, environment: str) -> None:
        self._environment = environment

    async def reserve_slot(self) -> bool:
        self.slot_requests += 1
        return self.slot_available

    async def count_pending(self, broker_ticker: str) -> PendingOrderCount:
        self.pending_counts += 1
        return PendingOrderCount(
            broker_ticker=broker_ticker,
            pending=self.pending_orders,
            read_ok=self.pending_read_ok,
            api_initiated=self.pending_api_initiated,
            error_category=None if self.pending_read_ok else "ProviderUnavailable",
        )

    async def submit(self, command: ExecutionCommand) -> BrokerAcknowledgement:
        # Recorded before any raise, so a test can prove a transmission was
        # attempted even when it failed.
        self.submitted += 1
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        if self.acknowledgement is not None:
            return self.acknowledgement
        return BrokerAcknowledgement(
            order=order_view(signed_quantity=command.signed_quantity),
            http_status=200,
            payload={"id": 500100, "status": "NEW"},
            rate_limit={"limit": 50, "remaining": 49, "period": "60"},
        )

    async def fetch_order(self, broker_order_id: str) -> BrokerOrderView | None:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.orders.get(broker_order_id)

    async def find_candidates(
        self, *, broker_ticker: str, since: dt.datetime, until: dt.datetime
    ) -> CandidateSearch:
        self.searches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.search


def acknowledgement_for(
    order: BrokerOrderView, *, http_status: int = 200, payload: dict[str, Any] | None = None
) -> BrokerAcknowledgement:
    return BrokerAcknowledgement(
        order=order,
        http_status=http_status,
        payload=payload or {"id": int(order.broker_order_id), "status": order.status},
        rate_limit={},
    )


# ---------------------------------------------------------------------------
# World builders
#
# Shared between `test_execution.py`, `test_execution_concurrency.py` and
# `test_pending_order_limit.py`. Kept here rather than in one of them so a
# second module does not have to import a test file to get at them.
# ---------------------------------------------------------------------------
WEB_ACTOR = "web:local-operator"


def execution_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        # The master switch. Default false, so a deployment does not start
        # sending the moment Phase 8 lands; every test that expects a
        # transmission turns it on deliberately.
        "t212_execution_enabled": True,
    }
    base.update(overrides)
    return helpers.settings(**base)


def build(
    database: Database,
    settings: Settings,
    *,
    provider: FakeProvider | None = None,
    market_data: helpers.StubMarketData | None = None,
) -> tuple[ProposalService, ExecutionService, FakeProvider]:
    control = ControlStateService(database)
    proposals = helpers.service_with(database, settings, market_data=market_data, control=control)
    broker = provider or FakeProvider(_environment=settings.t212_env.value)
    execution = ExecutionService(
        database,
        settings,
        proposals=proposals,
        provider=broker,
        control=control,
        broker=Broker.TRADING212,
    )
    return proposals, execution, broker


async def authorized(
    database: Database,
    settings: Settings,
    *,
    provider: FakeProvider | None = None,
    market_data: helpers.StubMarketData | None = None,
    source: AuthorizationSource = AuthorizationSource.HUMAN_WEB,
    positions: dict[str, tuple[Decimal, Decimal]] | None = None,
    action: ThesisAction = ThesisAction.BUY,
) -> tuple[ProposalService, ExecutionService, FakeProvider, uuid.UUID]:
    """Seed a world and drive one proposal all the way to APPROVED."""
    proposals, execution, broker = build(
        database, settings, provider=provider, market_data=market_data
    )
    await helpers.seed(database, action=action)
    # The account snapshot has to come from the environment under test, or
    # `account_state_available` blocks before anything interesting happens.
    await helpers.fund(database, positions=positions, environment=settings.t212_env.value)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None, generated.reason
    if not generated.authorized:
        await proposals.authorize(
            generated.proposal_id,
            source=source,
            actor=WEB_ACTOR if source is AuthorizationSource.HUMAN_WEB else "telegram:4242",
        )
    return proposals, execution, broker, generated.proposal_id
