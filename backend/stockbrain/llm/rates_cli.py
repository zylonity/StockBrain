"""Look up published token prices and print them as StockBrain configuration.

An authoring aid, not a runtime component. Nothing in the application imports
this module, and the spend guard never reaches the network: rates are read from
configuration that a human pasted in and reviewed.

That separation is deliberate. Fetching prices at runtime would make the daily
and monthly spend ceilings depend on a third party being reachable, honest and
current -- so an aggregator's stale or wrong number would quietly move a real
money limit, and an outage would leave the guard with nothing to compare
against. Publishing a price is also not the same as the price *your* account is
billed at, which no public catalogue can know.

So this prints; it does not configure. Check the numbers against your provider's
own pricing page before pasting them into ``.env``.

Source: OpenRouter's public model catalogue
(``GET https://openrouter.ai/api/v1/models``), which is unauthenticated and
reports ``pricing.prompt``, ``pricing.completion`` and ``pricing.input_cache_read``
in USD **per token**. StockBrain configures USD per million tokens, so every
figure is multiplied by 1,000,000 here.

Usage::

    python -m stockbrain.llm.rates_cli deepseek/deepseek-v4-flash
    python -m stockbrain.llm.rates_cli --search muse-spark
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import Any

import httpx

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"

_PER_MILLION = Decimal(1_000_000)


def _rate(pricing: dict[str, Any], key: str) -> Decimal | None:
    """USD per million tokens for one pricing key, or ``None`` if absent.

    A missing key means "not published", which is reported as such. It is not
    reported as zero: a zero rate in configuration would exempt that component
    of every call from the spend caps.
    """
    raw = pricing.get(key)
    if raw in (None, ""):
        return None
    try:
        return (Decimal(str(raw)) * _PER_MILLION).normalize()
    except (ArithmeticError, ValueError):
        return None


def fetch_models(timeout: float = 30.0) -> list[dict[str, Any]]:
    response = httpx.get(CATALOGUE_URL, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("unexpected catalogue shape: no 'data' list")
    return [entry for entry in data if isinstance(entry, dict)]


def render_env(entry: dict[str, Any], *, deep: bool = False) -> str:
    """Render one catalogue entry as StockBrain environment variables."""
    pricing = entry.get("pricing") or {}
    prefix = "LLM_DEEP" if deep else "LLM"
    lines = [f"# {entry.get('id')} -- {entry.get('name', '')}".rstrip(" -")]

    context = entry.get("context_length")
    if context:
        lines.append(f"# context window: {context:,} tokens")

    inp = _rate(pricing, "prompt")
    cached = _rate(pricing, "input_cache_read")
    out = _rate(pricing, "completion")

    if inp is None or out is None:
        lines.append("# incomplete pricing published; check the provider's own page")

    lines.append(f"{prefix}_INPUT_USD_PER_MTOK={inp if inp is not None else ''}")
    if cached is None:
        lines.append(
            f"# no cached-input rate published; leaving {prefix}_CACHED_INPUT_USD_PER_MTOK "
            "unset bills cached tokens at the full input rate, which over-estimates"
        )
        lines.append(f"# {prefix}_CACHED_INPUT_USD_PER_MTOK=")
    else:
        lines.append(f"{prefix}_CACHED_INPUT_USD_PER_MTOK={cached}")
    lines.append(f"{prefix}_OUTPUT_USD_PER_MTOK={out if out is not None else ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_id", nargs="?", help="exact catalogue model id")
    parser.add_argument("--search", help="substring match against model ids")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="emit LLM_DEEP_* variables instead of LLM_*",
    )
    parser.add_argument("--json", action="store_true", help="print the raw catalogue entry")
    arguments = parser.parse_args(argv)

    if not arguments.model_id and not arguments.search:
        parser.error("give a model id or --search")

    models = fetch_models()

    if arguments.search:
        needle = arguments.search.lower()
        matches = [m for m in models if needle in str(m.get("id", "")).lower()]
        if not matches:
            print(f"no catalogue entry matches {arguments.search!r}", file=sys.stderr)  # noqa: T201
            return 1
        for match in matches:
            pricing = match.get("pricing") or {}
            inp = _rate(pricing, "prompt")
            out = _rate(pricing, "completion")
            print(  # noqa: T201 - operator CLI output
                f"{match.get('id')}\tin={inp or '?'}\tout={out or '?'} (USD/1M tokens)"
            )
        return 0

    entry: dict[str, Any] | None = next(
        (m for m in models if m.get("id") == arguments.model_id), None
    )
    if entry is None:
        print(f"no catalogue entry with id {arguments.model_id!r}", file=sys.stderr)  # noqa: T201
        return 1

    if arguments.json:
        print(json.dumps(entry, indent=2, sort_keys=True))  # noqa: T201
        return 0

    print(render_env(entry, deep=arguments.deep))  # noqa: T201
    print(  # noqa: T201
        "\n# Verify against the provider's own pricing page before use, and record\n"
        "# the date and source in docs/sources.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
