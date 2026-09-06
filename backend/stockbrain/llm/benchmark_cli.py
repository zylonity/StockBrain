"""Small A/B harness: run the same fixed cases through two or more backends.

This exists so provider choice can be an empirical decision rather than an
argument. It is deliberately not a benchmarking subsystem: no scheduler, no
database writes, no scoring, no verdict. It runs each case through each arm once
and prints machine-readable rows for something else to analyse.

What it does **not** do is judge model quality. A handful of cases proves API
compatibility and exposes obvious degradation; it does not establish that one
model classifies better than another, and this tool reports no winner.

Two safety properties:

* it writes nothing -- no ``llm_calls`` rows, no events, no classifications, so
  a benchmark run cannot pollute the spend history or the event stream
* it is bounded by ``--limit`` and runs arms sequentially per case, so a typo
  cannot start an unbounded paid loop

Arms are described in a small JSON file so no credential is ever passed on the
command line, where it would land in shell history::

    [
      {
        "name": "deepseek",
        "provider": "deepseek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
        "input_usd_per_mtok": "0.44",
        "cached_input_usd_per_mtok": "0.014",
        "output_usd_per_mtok": "1.32"
      },
      {
        "name": "muse",
        "provider": "meta",
        "api_key_env": "MUSE_API_KEY",
        "model": "muse-spark-1.3-contributor",
        "input_usd_per_mtok": "0.10",
        "cached_input_usd_per_mtok": "0.002",
        "output_usd_per_mtok": "0.20"
      }
    ]

Cases are JSON Lines, one object per case, with the fields
:class:`~stockbrain.intelligence.classifier.ClassificationInput` accepts plus an
``id``::

    {"id": "case-1", "headline": "...", "body": "...", "provider": "brave"}

Usage::

    python -m stockbrain.llm.benchmark_cli --arms arms.json --cases cases.jsonl --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from stockbrain.intelligence.classifier import ClassificationInput, EventClassifier
from stockbrain.llm.openai_compat import OpenAICompatibleClient
from stockbrain.llm.pricing import DEFAULT_RATES, ModelRates, PricingTable
from stockbrain.llm.profiles import profile_for

#: Hard ceiling on cases per run, independent of --limit. A benchmark that can
#: be pointed at an unbounded file is a way to spend money by accident.
MAX_CASES = 50


@dataclass(frozen=True, slots=True)
class Arm:
    """One backend under test."""

    name: str
    provider: str
    api_key: str
    model: str
    base_url: str | None
    rates: ModelRates | None

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> Arm:
        key_env = raw.get("api_key_env")
        if not key_env:
            raise ValueError(f"arm {raw.get('name')!r}: api_key_env is required")
        api_key = os.environ.get(str(key_env), "")
        if not api_key:
            raise ValueError(f"arm {raw.get('name')!r}: environment variable {key_env} is not set")

        rates: ModelRates | None = None
        if raw.get("input_usd_per_mtok") is not None:
            inp = Decimal(str(raw["input_usd_per_mtok"]))
            rates = ModelRates(
                cache_hit_input=Decimal(str(raw.get("cached_input_usd_per_mtok", inp))),
                cache_miss_input=inp,
                output=Decimal(str(raw.get("output_usd_per_mtok", inp))),
                off_peak_multiplier=Decimal(str(raw.get("off_peak_multiplier", "1"))),
            )

        return cls(
            name=str(raw.get("name") or raw["provider"]),
            provider=str(raw["provider"]),
            api_key=api_key,
            model=str(raw["model"]),
            base_url=raw.get("base_url"),
            rates=rates,
        )

    def build_client(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            profile_for(self.provider),
            api_key=self.api_key,
            base_url=self.base_url,
            max_attempts=1,  # one attempt per case: a retry would skew latency
        )

    def pricing(self) -> PricingTable:
        rates = dict(DEFAULT_RATES)
        if self.rates is not None:
            rates[self.model] = self.rates
        return PricingTable(rates)


def load_arms(path: Path) -> list[Arm]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("arms file must be a non-empty JSON list")
    return [Arm.from_config(entry) for entry in raw]


def load_cases(path: Path, limit: int) -> list[tuple[str, ClassificationInput]]:
    cases: list[tuple[str, ClassificationInput]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        if len(cases) >= limit:
            break
        raw = json.loads(line)
        case_id = str(raw.pop("id", f"line-{line_number}"))
        published = raw.pop("published_at", None)
        cases.append(
            (
                case_id,
                ClassificationInput(
                    headline=raw.get("headline"),
                    body=raw.get("body"),
                    provider=str(raw.get("provider", "benchmark")),
                    source_name=raw.get("source_name"),
                    source_category=raw.get("source_category"),
                    url=raw.get("url"),
                    published_at=(dt.datetime.fromisoformat(published) if published else None),
                    symbol_hints=raw.get("symbol_hints"),
                ),
            )
        )
    return cases


async def run_case(arm: Arm, case_id: str, payload: ClassificationInput) -> dict[str, Any]:
    """One case against one arm. Never raises: a failure is a recorded row."""
    client = arm.build_client()
    classifier = EventClassifier(client, model=arm.model)
    pricing = arm.pricing()

    row: dict[str, Any] = {
        "case_id": case_id,
        "arm": arm.name,
        "provider": arm.provider,
        "model": arm.model,
    }
    started = time.monotonic()
    try:
        outcome = await classifier.classify(payload)
    except Exception as exc:
        row.update(
            schema_ok=False,
            ok=False,
            error_class=type(exc).__name__,
            # Message only: provider exceptions are constructed without
            # credentials, but the excerpt is bounded regardless.
            error=str(exc)[:300],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return row
    finally:
        await client.aclose()

    usage = outcome.result.usage
    cost = pricing.estimate(outcome.result.model or arm.model, usage)
    classification = outcome.classification
    row.update(
        ok=True,
        schema_ok=True,
        latency_ms=outcome.result.latency_ms,
        input_tokens=usage.prompt_tokens,
        cached_input_tokens=usage.cache_hit_tokens,
        billable_input_tokens=usage.billable_cache_miss,
        output_tokens=usage.completion_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        # Recorded as a flag only. Hidden reasoning is never printed or stored.
        had_reasoning_content=outcome.result.had_reasoning_content,
        estimated_cost_usd=str(cost) if cost is not None else None,
        finish_reason=outcome.result.finish_reason,
        # The classification itself, so score distributions can be compared
        # across arms. Nothing here is a verdict on model quality.
        relevant_to_public_equities=classification.relevant_to_public_equities,
        event_type=classification.event_type,
        canonical_title=classification.canonical_title,
        novelty=classification.novelty,
        importance=classification.importance,
        confidence=classification.confidence,
        needs_corroboration=classification.needs_corroboration,
        companies=[
            {
                "company_name": company.company_name,
                "ticker_hint": company.ticker_hint,
                "impact_path": company.impact_path,
                "direction": company.direction,
                "materiality": company.materiality,
                "confidence": company.confidence,
            }
            for company in classification.companies
        ],
    )
    return row


async def run(
    arms: list[Arm], cases: list[tuple[str, ClassificationInput]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id, payload in cases:
        for arm in arms:
            # Sequential on purpose: concurrent arms would contend for the same
            # rate limit and make the latency column meaningless.
            rows.append(await run_case(arm, case_id, payload))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B a fixed case set across LLM backends.")
    parser.add_argument("--arms", type=Path, required=True, help="JSON file describing each arm")
    parser.add_argument("--cases", type=Path, required=True, help="JSON Lines case file")
    parser.add_argument("--limit", type=int, default=20, help=f"cases to run (max {MAX_CASES})")
    parser.add_argument("--out", type=Path, help="write JSON rows here instead of stdout")
    arguments = parser.parse_args(argv)

    if arguments.limit < 1 or arguments.limit > MAX_CASES:
        parser.error(f"--limit must be between 1 and {MAX_CASES}")

    arms = load_arms(arguments.arms)
    cases = load_cases(arguments.cases, arguments.limit)
    if not cases:
        print("no cases to run", file=sys.stderr)  # noqa: T201
        return 1

    print(  # noqa: T201
        f"running {len(cases)} case(s) x {len(arms)} arm(s) = "
        f"{len(cases) * len(arms)} paid call(s)",
        file=sys.stderr,
    )
    rows = asyncio.run(run(arms, cases))

    payload = json.dumps(rows, indent=2, sort_keys=True)
    if arguments.out:
        arguments.out.write_text(payload + "\n")
        print(f"wrote {len(rows)} rows to {arguments.out}", file=sys.stderr)  # noqa: T201
    else:
        print(payload)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
