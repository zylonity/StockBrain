"""Versioned prompt loading and safe rendering.

Prompts live in ``prompts/<name>/<version>.md`` under source control, and the
version is recorded on every LLM call, so an answer produced by v1 stays
traceable after v2 ships.

Two properties matter:

* **Substitution is one-way.** Rendering replaces ``{{TOKEN}}`` placeholders with
  caller-supplied values in a single pass. Substituted text is never re-scanned,
  so a document containing the literal text ``{{DOCUMENT}}`` cannot inject
  another placeholder's contents.
* **Untrusted text is fenced and length-bounded.** Document bodies are wrapped by
  the prompt template in ``<untrusted_document>`` markers and truncated here, so
  a very long scraped page cannot push the instructions out of the context.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from stockbrain.logging import get_logger

__all__ = [
    "MAX_DOCUMENT_CHARS",
    "PromptTemplate",
    "load_prompt",
    "sanitize_untrusted",
    "split_system_user",
]

log = get_logger(__name__)

#: Repository root, resolved from this file: backend/stockbrain/intelligence -> repo.
_PROMPT_ROOT_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "prompts",
    Path(__file__).resolve().parents[2] / "prompts",
)

#: Truncation limit for one untrusted document. Generous enough for a long
#: article, small enough that instructions always survive in context.
MAX_DOCUMENT_CHARS = 24_000

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

#: Sequences that would let document text escape its fence and appear to be part
#: of the prompt structure. Neutralised rather than removed, so the attempt stays
#: visible to the model as evidence.
_FENCE_ESCAPES = (
    ("</untrusted_document>", "<­/untrusted_document>"),
    ("<untrusted_document>", "<­untrusted_document>"),
    ("</candidate_event>", "<­/candidate_event>"),
    ("<candidate_event>", "<­candidate_event>"),
)


def _prompt_root() -> Path:
    for candidate in _PROMPT_ROOT_CANDIDATES:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "prompts directory not found; expected one of: "
        + ", ".join(str(path) for path in _PROMPT_ROOT_CANDIDATES)
    )


class PromptTemplate:
    """A loaded prompt, split into its system and user halves."""

    def __init__(self, name: str, version: str, system: str, user: str) -> None:
        self.name = name
        self.version = version
        self.system = system
        self.user = user

    @property
    def identifier(self) -> str:
        """Stored on every LLM call, e.g. ``event_classifier/v1``."""
        return f"{self.name}/{self.version}"

    def render(self, values: dict[str, str]) -> tuple[str, str]:
        """Substitute placeholders in one pass and return ``(system, user)``.

        A missing placeholder is an error rather than a silently empty prompt:
        sending the model a template with ``{{DOCUMENT}}`` still in it would
        produce a confident classification of nothing.
        """
        return (
            _substitute(self.system, values, self.identifier),
            _substitute(self.user, values, self.identifier),
        )


def _substitute(template: str, values: dict[str, str], identifier: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in values:
            missing.append(token)
            return match.group(0)
        return values[token]

    # One pass: re.sub does not rescan replacement text, so a value containing
    # "{{OTHER}}" is inert rather than being expanded.
    result = _PLACEHOLDER_RE.sub(replace, template)
    if missing:
        raise KeyError(f"prompt {identifier} is missing values for: {sorted(set(missing))}")
    return result


def split_system_user(markdown: str) -> tuple[str, str]:
    """Extract the ``## SYSTEM`` and ``## USER`` sections of a prompt file."""
    system_match = re.search(r"^## SYSTEM\s*$(.*?)^## USER\s*$", markdown, re.MULTILINE | re.DOTALL)
    if system_match is None:
        raise ValueError("prompt file must contain '## SYSTEM' followed by '## USER'")
    user_start = markdown.index("## USER", system_match.start())
    user = markdown[user_start + len("## USER") :]

    system = system_match.group(1)
    # Trailing "---" separators are formatting, not content.
    system = re.sub(r"\n---\s*\n?$", "\n", system.strip())
    user = re.sub(r"\n---\s*\n?$", "\n", user.strip())
    return system.strip(), user.strip()


@functools.lru_cache(maxsize=32)
def load_prompt(name: str, version: str = "v1") -> PromptTemplate:
    """Load and cache ``prompts/<name>/<version>.md``."""
    if not re.fullmatch(r"[a-z0-9_]+", name) or not re.fullmatch(r"v[0-9]+", version):
        raise ValueError(f"invalid prompt reference {name}/{version}")

    path = _prompt_root() / name / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")

    system, user = split_system_user(path.read_text(encoding="utf-8"))
    log.debug("prompt_loaded", prompt=f"{name}/{version}", system_chars=len(system))
    return PromptTemplate(name=name, version=version, system=system, user=user)


def sanitize_untrusted(text: str | None, *, limit: int = MAX_DOCUMENT_CHARS) -> str:
    """Prepare untrusted text for inclusion inside a fenced prompt block.

    Neutralises fence-closing sequences and truncates. The text is *not*
    otherwise rewritten: the model needs to see injection attempts in order to
    flag them, and silently stripping them would hide a signal about source
    quality.
    """
    if not text:
        return "(no content)"

    cleaned = text.replace("\x00", "")
    for needle, replacement in _FENCE_ESCAPES:
        # Case-insensitive: "</UNTRUSTED_DOCUMENT>" closes the fence too.
        cleaned = re.sub(re.escape(needle), replacement, cleaned, flags=re.IGNORECASE)

    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n\n[truncated by StockBrain]"
    return cleaned
