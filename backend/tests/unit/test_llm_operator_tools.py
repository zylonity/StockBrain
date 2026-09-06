"""The two operator-facing LLM tools: rate lookup and the A/B harness.

Neither is reachable from the running application, so these cover the parsing
and rendering that an operator would otherwise only discover was wrong while
holding a bill.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from stockbrain.llm.benchmark_cli import MAX_CASES, Arm, load_arms, load_cases
from stockbrain.llm.rates_cli import _rate, render_env

# ---------------------------------------------------------------------------
# Rate lookup
# ---------------------------------------------------------------------------


def test_per_token_prices_are_converted_to_per_million() -> None:
    """Catalogues quote USD per token; StockBrain configures per million."""
    assert _rate({"prompt": "0.0000001"}, "prompt") == Decimal("0.1")
    assert _rate({"completion": "0.0000002"}, "completion") == Decimal("0.2")
    assert _rate({"input_cache_read": "0.000000002"}, "input_cache_read") == Decimal("0.002")


def test_an_unpublished_rate_is_none_and_never_zero() -> None:
    """A zero rate in configuration would exempt that component from the caps."""
    assert _rate({}, "prompt") is None
    assert _rate({"prompt": ""}, "prompt") is None
    assert _rate({"prompt": "not-a-number"}, "prompt") is None


def test_render_env_emits_pasteable_configuration() -> None:
    rendered = render_env(
        {
            "id": "meta/muse-spark-1.3-contributor",
            "name": "Muse Spark 1.3 Contributor",
            "context_length": 1_048_576,
            "pricing": {
                "prompt": "0.0000001",
                "completion": "0.0000002",
                "input_cache_read": "0.000000002",
            },
        }
    )
    assert "LLM_INPUT_USD_PER_MTOK=0.1" in rendered
    assert "LLM_CACHED_INPUT_USD_PER_MTOK=0.002" in rendered
    assert "LLM_OUTPUT_USD_PER_MTOK=0.2" in rendered
    assert "1,048,576 tokens" in rendered


def test_a_missing_cache_rate_says_what_leaving_it_unset_costs() -> None:
    rendered = render_env(
        {"id": "x/y", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    )
    # Commented out rather than emitted empty, and the consequence is stated.
    assert "# LLM_CACHED_INPUT_USD_PER_MTOK=" in rendered
    assert "over-estimates" in rendered


def test_deep_role_variables_are_rendered_on_request() -> None:
    rendered = render_env(
        {"id": "x/y", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}, deep=True
    )
    assert "LLM_DEEP_INPUT_USD_PER_MTOK=1" in rendered
    assert "LLM_DEEP_OUTPUT_USD_PER_MTOK=2" in rendered


# ---------------------------------------------------------------------------
# A/B harness
# ---------------------------------------------------------------------------


def test_an_arm_reads_its_key_from_the_environment_not_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key on the command line lands in shell history."""
    monkeypatch.setenv("BENCH_KEY", "sk-bench-not-real")
    arm = Arm.from_config(
        {
            "name": "muse",
            "provider": "meta",
            "api_key_env": "BENCH_KEY",
            "model": "muse-spark-1.3-contributor",
            "input_usd_per_mtok": "0.10",
            "cached_input_usd_per_mtok": "0.002",
            "output_usd_per_mtok": "0.20",
        }
    )
    assert arm.api_key == "sk-bench-not-real"
    assert arm.pricing().rates_for("muse-spark-1.3-contributor") is not None


def test_an_arm_with_no_key_configured_fails_before_spending_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BENCH_MISSING", raising=False)
    with pytest.raises(ValueError, match="is not set"):
        Arm.from_config(
            {"name": "x", "provider": "generic", "api_key_env": "BENCH_MISSING", "model": "m"}
        )


def test_an_arm_without_rates_still_runs_but_prices_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness reports cost as null rather than inventing one."""
    monkeypatch.setenv("BENCH_KEY", "k")
    arm = Arm.from_config(
        {"name": "x", "provider": "generic", "api_key_env": "BENCH_KEY", "model": "unknown-model"}
    )
    assert arm.rates is None
    assert arm.pricing().rates_for("unknown-model") is None


def test_arms_and_cases_load_from_their_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BENCH_KEY", "k")
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(
        json.dumps(
            [
                {
                    "name": "deepseek",
                    "provider": "deepseek",
                    "api_key_env": "BENCH_KEY",
                    "model": "deepseek-v4-flash",
                }
            ]
        )
    )
    assert [arm.name for arm in load_arms(arms_file)] == ["deepseek"]

    cases_file = tmp_path / "cases.jsonl"
    cases_file.write_text(
        '{"id": "a", "headline": "H1", "body": "B1", "provider": "brave", '
        '"published_at": "2026-09-01T00:00:00+00:00"}\n'
        "\n"
        '{"id": "b", "headline": "H2", "body": "B2", "provider": "exa"}\n'
    )
    cases = load_cases(cases_file, limit=10)
    assert [case_id for case_id, _ in cases] == ["a", "b"]
    assert cases[0][1].published_at is not None
    assert cases[1][1].published_at is None


def test_the_case_limit_is_enforced_so_a_typo_cannot_start_a_paid_loop(
    tmp_path: Path,
) -> None:
    cases_file = tmp_path / "cases.jsonl"
    cases_file.write_text(
        "\n".join(json.dumps({"id": str(n), "headline": "h", "body": "b"}) for n in range(100))
    )
    assert len(load_cases(cases_file, limit=5)) == 5
    assert MAX_CASES == 50


def test_an_empty_arms_file_is_refused(tmp_path: Path) -> None:
    arms_file = tmp_path / "arms.json"
    arms_file.write_text("[]")
    with pytest.raises(ValueError, match="non-empty"):
        load_arms(arms_file)


def test_an_arm_missing_its_key_variable_name_is_refused() -> None:
    with pytest.raises(ValueError, match="api_key_env is required"):
        Arm.from_config({"name": "x", "provider": "generic", "model": "m"})
