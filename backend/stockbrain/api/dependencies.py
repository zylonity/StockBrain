"""FastAPI dependency providers.

Shared objects (database, health registry) live on ``app.state`` and are
resolved from the request, which keeps them swappable in tests without global
monkeypatching.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.observability.health import ProviderHealthRegistry

__all__ = [
    "DatabaseDep",
    "DbSession",
    "HealthRegistry",
    "SettingsDep",
    "get_database",
    "get_health_registry",
    "get_session",
    "get_settings_dep",
]


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_health_registry(request: Request) -> ProviderHealthRegistry:
    registry: ProviderHealthRegistry = request.app.state.health_registry
    return registry


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


DatabaseDep = Annotated[Database, Depends(get_database)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
HealthRegistry = Annotated[ProviderHealthRegistry, Depends(get_health_registry)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
