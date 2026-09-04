"""Standalone configuration validation.

Run as ``python -m stockbrain.validate_config``.  The container entrypoint calls
this *before* waiting for the database, because a misconfiguration is never
transient and must not be retried in a connectivity loop.

Prints a readable summary of the execution posture on success and a single
clear error block on failure -- never a stack trace, and never a secret value.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from stockbrain.config import Settings

__all__ = ["main"]


def main() -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        print("CONFIGURATION ERROR: StockBrain refuses to start.", file=sys.stderr)
        print("", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(model)"
            # `msg` carries the explanation; `input` may contain secrets and is
            # deliberately not printed.
            message = error["msg"].removeprefix("Value error, ")
            print(f"  * {location}: {message}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Fix the environment or .env file and restart.", file=sys.stderr)
        return 1

    print(f"configuration OK (app_env={settings.app_env.value})")
    print(f"  broker environment:       {settings.t212_env.value}")
    print(f"  execution mode:           {settings.execution_mode.value}")
    print(f"  live execution permitted: {settings.live_execution_permitted}")
    for blocker in settings.execution_blockers:
        print(f"    - blocked: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
