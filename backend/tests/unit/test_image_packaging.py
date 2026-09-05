"""What the container image must actually contain to work.

A missing runtime *asset* fails differently from a missing dependency: the
process starts, reports healthy, serves the API, and one subsystem quietly does
nothing. These tests exist because that is exactly what happened.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_prompt_templates_are_packaged_into_the_image() -> None:
    """Regression: the container had no `prompts/` directory at all.

    ``load_prompt`` reads Markdown at runtime, and ``.dockerignore`` excluded
    ``*.md``. Every ``CLASSIFY_EVENT`` job therefore raised ``FileNotFoundError``
    *after* claiming its event; the retry then found the event already
    ``CLASSIFYING`` and reported success, so classification failed invisibly.
    The container looked healthy while 144 events sat in ``CLASSIFYING`` and no
    thesis -- and so no proposal -- could ever be produced.

    Two independent statements, because either one alone would let it recur: the
    Dockerfile must copy the directory, and the ignore file must not strip the
    Markdown back out of it.
    """
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY prompts ./prompts" in dockerfile

    ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!prompts/**/*.md" in ignore


def test_the_image_build_verifies_the_prompts_load() -> None:
    """The build fails loudly rather than shipping an image that cannot classify."""
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "from stockbrain.intelligence.prompts import load_prompt" in dockerfile


def test_every_prompt_the_code_asks_for_actually_exists() -> None:
    """A prompt reference that resolves nowhere is a runtime failure, not a typo."""
    from stockbrain.intelligence.prompts import load_prompt

    for name in ("event_classifier", "event_dedupe"):
        template = load_prompt(name)
        assert template.system and template.user
        assert template.identifier == f"{name}/v1"
