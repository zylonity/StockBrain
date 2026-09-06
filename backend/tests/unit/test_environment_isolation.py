"""The operator's `.env` must never set a test's premise.

Nulling ``Settings.model_config["env_file"]`` looks like it should be enough,
and it is not: by the time any fixture runs, something may already have merged
the file into ``os.environ``, where a leaked value is indistinguishable from a
deliberate one.  The pinned upstream does exactly that --
``third_party/TradingAgents/tradingagents/__init__.py`` calls
``load_dotenv(find_dotenv(usecwd=True))`` at import time, and ``usecwd=True``
walks up from ``backend/`` until it finds the repository ``.env``.

The consequence was a suite that passed by *luck*: as long as a developer's
`.env` agreed with the test defaults, nothing showed.  When one set
``T212_LIVE_EXECUTION_ENABLED=true`` and ``FX_PROVIDER=frankfurter`` for their
own deployment, 144 tests began failing on their machine and nowhere else, in
files that had nothing to do with either setting.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import (
    _HARNESS_OWNED_ENVIRONMENT,
    _REPOSITORY_ENV_NAMES,
    dotenv_variable_names,
)


# ---------------------------------------------------------------------------
# The invariant itself
# ---------------------------------------------------------------------------
def test_no_variable_from_the_operator_env_file_is_visible_to_a_test() -> None:
    """Nothing the developer configured for their deployment reaches a test.

    This is strongest in a full run, where the research tests have already
    imported the upstream package and populated ``os.environ`` -- but it holds
    in a targeted run too, because anything inherited from the developer's
    shell is scrubbed by the same fixture.
    """
    leaked = sorted(name for name in _REPOSITORY_ENV_NAMES if name in os.environ)

    assert leaked == [], (
        "the repository .env reached os.environ and survived into a test: "
        f"{leaked}. A developer's deployment settings must not decide what a "
        "test observes."
    )


def test_the_database_url_the_harness_owns_is_deliberately_kept() -> None:
    """The one exception, asserted so it stays a decision rather than a bug.

    ``alembic/env.py`` rebuilds ``sqlalchemy.url`` from ``get_settings()``, so
    migrations find the test database only via this variable.
    """
    assert "DATABASE_URL" in _HARNESS_OWNED_ENVIRONMENT
    assert "DATABASE_URL" not in _REPOSITORY_ENV_NAMES


# ---------------------------------------------------------------------------
# The parser the scrubbing list is built from
# ---------------------------------------------------------------------------
def test_variable_names_are_read_from_a_dotenv_file(tmp_path: Path) -> None:
    """Real dotenv shapes: comments, blanks, quotes, `export`, inline `#`."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "T212_ENV=demo                 # trailing comment",
                "FX_PROVIDER=frankfurter",
                "  SPACED_NAME = value  ",
                "export EXPORTED_NAME=1",
                'QUOTED="a value with = in it"',
                "  # an indented comment",
                "NOT_AN_ASSIGNMENT",
                "not-an-identifier=1",
            ]
        ),
        encoding="utf-8",
    )

    assert dotenv_variable_names(env_file) == {
        "T212_ENV",
        "FX_PROVIDER",
        "SPACED_NAME",
        "EXPORTED_NAME",
        "QUOTED",
    }


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """CI has no `.env`, and must not need one to run the suite."""
    assert dotenv_variable_names(tmp_path / "nonexistent") == frozenset()


def test_only_names_are_read_never_values(tmp_path: Path) -> None:
    """A value from the operator's file must not travel anywhere.

    Reading one would be the mistake the scrubbing exists to prevent, so the
    return type carries names alone by construction.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("T212_API_KEY=super-secret-value\n", encoding="utf-8")

    names = dotenv_variable_names(env_file)

    assert names == {"T212_API_KEY"}
    assert not any("super-secret-value" in name for name in names)


def test_the_scrub_list_covers_the_settings_that_actually_broke_the_suite() -> None:
    """A regression guard naming the two culprits.

    Skipped rather than asserted when the developer has no `.env` (CI), because
    there is then nothing that could leak.
    """
    if not _REPOSITORY_ENV_NAMES:
        return
    from tests.conftest import REPOSITORY_ENV_FILE

    defined = dotenv_variable_names(REPOSITORY_ENV_FILE)
    for name in ("T212_LIVE_EXECUTION_ENABLED", "FX_PROVIDER"):
        if name in defined:
            assert name in _REPOSITORY_ENV_NAMES, f"{name} must be scrubbed before a test"
