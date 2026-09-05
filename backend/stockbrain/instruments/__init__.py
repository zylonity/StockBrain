"""Company -> broker instrument resolution.

An LLM's ticker hint is a search key, never an identity.  Everything here exists
to make sure the only identifier that can reach an order request is one Trading
212 itself supplied.
"""

from stockbrain.instruments.aliases import AliasSpec, alias_key, upsert_alias
from stockbrain.instruments.resolver import (
    InstrumentCandidate,
    InstrumentResolver,
    ResolutionRequest,
    ResolutionResult,
)
from stockbrain.instruments.service import ResolutionRunResult, ResolutionService

__all__ = [
    "AliasSpec",
    "InstrumentCandidate",
    "InstrumentResolver",
    "ResolutionRequest",
    "ResolutionResult",
    "ResolutionRunResult",
    "ResolutionService",
    "alias_key",
    "upsert_alias",
]
