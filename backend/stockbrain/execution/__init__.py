"""Broker order transmission, and the machinery that makes it survivable.

Layering, outermost first:

``service``         claim, preflight, transmit once, classify, persist
``reconciliation``  resolve an ambiguous attempt by reading, never by sending
``preflight``       every pre-send check, refusal by default
``base``            the four-operation broker interface, and the T212 adapter
``fingerprint``     a deterministic audit hash -- explicitly not an idempotency key
``models``          the broker-neutral vocabulary

The invariant the whole package exists to hold: **at most one order per
proposal, ever**, enforced by ``uq_execution_attempts_sent_once`` with
``sent_to_broker`` written *before* the HTTP request rather than after the
response.  Everything else here follows from taking that ordering seriously.
"""

from stockbrain.execution.base import BrokerExecutionProvider, Trading212ExecutionProvider
from stockbrain.execution.models import ExecutionCommand
from stockbrain.execution.reconciliation import ReconciliationService
from stockbrain.execution.service import ExecutionResult, ExecutionService

__all__ = [
    "BrokerExecutionProvider",
    "ExecutionCommand",
    "ExecutionResult",
    "ExecutionService",
    "ReconciliationService",
    "Trading212ExecutionProvider",
]
