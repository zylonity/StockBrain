"""A deterministic fingerprint of one broker mutation.

**This is not an idempotency key, and Trading 212 does not have one.**  The
order endpoint is documented as non-idempotent, accepts no client-supplied
reference, and will happily create a second identical order.  Nothing here makes
a resend safe, and no code may read it as though it did.

What it is for:

* **Audit.**  "Is the order the broker has the order we meant to send?" is
  answerable from a hash rather than from re-reading five columns.
* **Reconciliation evidence.**  Two attempts on the same listing, side and
  quantity within the same minute are indistinguishable at the broker; the
  fingerprint at least tells an operator whether *StockBrain* thought they were
  the same request.
* **Regression detection.**  A change to how the body is built changes the
  fingerprint, and a test pins the value for a known command.

It is computed from the immutable execution snapshot, so it covers the identity
of the trade (proposal, environment, listing, side, quantity, order type,
session flag) and deliberately *not* the wall clock or the attempt number -- two
attempts at the same trade should fingerprint the same, because that is the
collision an operator needs to see.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from stockbrain.execution.models import ExecutionCommand

__all__ = ["FINGERPRINT_VERSION", "fingerprint_for", "fingerprint_payload"]

#: Bumping this changes every fingerprint, so it is part of the hashed payload
#: rather than a comment: a stored fingerprint stays interpretable against the
#: scheme that produced it.
FINGERPRINT_VERSION = 1


def fingerprint_payload(command: ExecutionCommand) -> dict[str, Any]:
    """The exact fields hashed, in a stable shape.

    Money and quantities are strings, not floats: a fingerprint that depended on
    binary floating point would differ between machines for the same trade.
    """
    return {
        "version": FINGERPRINT_VERSION,
        "proposal_id": str(command.proposal_id),
        "broker": command.broker.value,
        "broker_environment": command.broker_environment,
        "broker_ticker": command.broker_ticker,
        "order_type": command.order_type.value,
        "side": command.side.value,
        "quantity": format(command.quantity, "f"),
        "signed_quantity": format(command.signed_quantity, "f"),
        "extended_hours": command.extended_hours,
    }


def fingerprint_for(command: ExecutionCommand) -> str:
    """SHA-256 over the canonical payload.

    ``sort_keys`` and a fixed separator set make the encoding independent of
    dictionary ordering, so the same command hashes the same across processes
    and across Python versions.  Nothing secret is in the payload -- a test
    asserts it -- so the fingerprint is safe to log and to return over the API.
    """
    encoded = json.dumps(
        fingerprint_payload(command), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
