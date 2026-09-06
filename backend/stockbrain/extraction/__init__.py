"""Page content extraction, kept separate from discovery.

Discovery decides *what exists*; extraction decides *what to read*.  Firecrawl
merged the two behind one credential, and the consequence was that a thematic
search paid to fetch every result page before anything had judged one worth
reading.  The split here is what makes the cheap-first pipeline expressible:
search returns metadata, deduplication and the classifier triage run on that
metadata, and only a shortlisted URL is ever fetched -- locally first, and
through a paid provider only when local extraction failed for a reason a
different fetcher could fix.
"""

from __future__ import annotations

from stockbrain.extraction.base import (
    ContentExtractor,
    ExtractionFailure,
    ExtractionResult,
)
from stockbrain.extraction.ssrf import SsrfRefused, redact_url, verify_public_url

__all__ = [
    "ContentExtractor",
    "ExtractionFailure",
    "ExtractionResult",
    "SsrfRefused",
    "redact_url",
    "verify_public_url",
]
