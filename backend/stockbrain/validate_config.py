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

    from stockbrain.api.auth import web_auth_blockers

    print(f"configuration OK (app_env={settings.app_env.value})")
    print(f"  broker environment:       {settings.t212_env.value}")
    print(f"  execution mode:           {settings.execution_mode.value}")
    print(f"  live execution permitted: {settings.live_execution_permitted}")
    for blocker in settings.execution_blockers:
        print(f"    - blocked: {blocker}")
    print(f"  order transmission:       {settings.order_transmission_permitted}")
    for blocker in settings.order_transmission_blockers:
        print(f"    - blocked: {blocker}")

    # The three Phase 9 postures. Printed here because this command is the
    # pre-deploy gate, and each of them is a way a deployment can look correct
    # and behave differently from what the operator intended.
    auth_blockers = web_auth_blockers(settings)
    print(f"  web authentication:       {'enforced' if not auth_blockers else 'NOT ENFORCED'}")
    for blocker in auth_blockers:
        print(f"    - {blocker}")

    print(f"  firecrawl:                {'enabled' if settings.firecrawl_available else 'off'}")
    for blocker in settings.firecrawl_blockers:
        print(f"    - {blocker}")
    if settings.firecrawl_available:
        # The projection, so an operator sees the spend before it happens rather
        # than after an HTTP 402.
        print(
            f"    caps: {settings.firecrawl_max_searches_per_day} searches/day, "
            f"{settings.firecrawl_max_scrapes_per_day} scrapes/day, "
            f"{settings.firecrawl_daily_credit_cap} credits/day, "
            f"{settings.firecrawl_monthly_credit_cap} credits/month"
        )
        print(
            f"    cadence: no topic faster than "
            f"{settings.firecrawl_min_topic_interval_minutes} minutes"
        )

    print(f"  fx provider:              {settings.fx_provider.value}")
    for blocker in settings.fx_blockers:
        print(f"    - {blocker}")
    print(f"  same-currency required:   {settings.risk_require_same_currency}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
