"""Trade proposals: their lifecycle, their authorization, and their retirement.

Nothing in this package sends a broker order.  A proposal reaching ``APPROVED``
records that deterministic risk allowed the trade and that a named authority --
a person on the web, or the system itself under an explicitly permitted
automatic policy -- signed it off.  Order transmission arrives in Phase 8,
behind its own gates.

This module is deliberately empty of imports.  ``db.models.proposals`` reads the
state machine's ``ACTIVE_STATUSES`` so the partial unique index and the
transition table cannot drift apart, and re-exporting the service here would
make that a circular import.  Import the submodules directly.
"""

from __future__ import annotations

__all__: list[str] = []
