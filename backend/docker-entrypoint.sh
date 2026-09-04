#!/bin/sh
# Container entrypoint.
#
# Ordering matters:
#
#   1. validate configuration  -- fail fast and loudly; a bad config is never
#      transient, so it must not be retried
#   2. wait for PostgreSQL     -- this one genuinely is transient
#   3. run migrations          -- explicitly, so a rollback to an older image
#      never silently upgrades a schema and a failed migration stops the
#      container rather than leaving a half-configured process serving traffic
#   4. start the application
set -eu

# Exit codes: 78 = EX_CONFIG (sysexits.h), so an orchestrator can distinguish a
# misconfiguration from a crash.
EXIT_CONFIG=78

validate_config() {
    if ! python -m stockbrain.validate_config; then
        exit "$EXIT_CONFIG"
    fi
}

database_ready() {
    python - <<'PY'
import asyncio
import sys

from sqlalchemy import text

from stockbrain.config import get_settings
from stockbrain.db.session import Database


async def probe() -> int:
    database = Database(get_settings())
    try:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not handle
        print(f"database not ready: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await database.dispose()
    return 0


sys.exit(asyncio.run(probe()))
PY
}

wait_for_database() {
    attempts=0
    max_attempts=${DB_WAIT_ATTEMPTS:-60}
    while ! database_ready; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge "$max_attempts" ]; then
            echo "database did not become ready after ${max_attempts} attempts" >&2
            exit 1
        fi
        sleep 2
    done
}

case "${1:-serve}" in
    serve)
        validate_config
        wait_for_database
        echo "running database migrations"
        alembic upgrade head
        echo "starting stockbrain"
        exec python -m stockbrain.main
        ;;
    migrate)
        validate_config
        wait_for_database
        exec alembic upgrade head
        ;;
    check-config)
        # Validate configuration and exit. Useful before a deploy.
        validate_config
        ;;
    shell)
        exec /bin/sh
        ;;
    *)
        exec "$@"
        ;;
esac
