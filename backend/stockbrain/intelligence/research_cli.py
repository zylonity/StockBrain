"""Operator-controlled research enqueue; UUID rerun tokens are durable and idempotent."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer


async def enqueue(impact_id: uuid.UUID, rerun_id: uuid.UUID | None) -> uuid.UUID:
    settings = Settings()
    database = Database(settings)
    services = ServiceContainer(settings, database, ProviderHealthRegistry())
    try:
        if services.research is None:
            raise RuntimeError("Research engine unavailable; inspect research health/configuration")
        async with database.transaction() as session:
            return await services.research.request(session, impact_id, rerun_id=rerun_id)
    finally:
        await services.stop()
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("impact_id", type=uuid.UUID)
    parser.add_argument(
        "--rerun-id",
        type=uuid.UUID,
        help="New UUID intentionally creates a new version; reuse it to deduplicate",
    )
    arguments = parser.parse_args()
    run_id = asyncio.run(enqueue(arguments.impact_id, arguments.rerun_id))
    print(f"Queued research {run_id}")  # noqa: T201 - operator CLI output


if __name__ == "__main__":
    main()
