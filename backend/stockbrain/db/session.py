"""Async engine and session management.

A single :class:`AsyncEngine` is created per process.  Sessions are short-lived
and never shared across tasks.  ``expire_on_commit=False`` keeps loaded objects
usable after a commit, which matters for the approval/execution paths where the
transaction boundary is deliberately narrow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from stockbrain.config import Settings, get_settings

__all__ = [
    "Database",
    "get_database",
    "reset_database",
]


class Database:
    """Owns the engine and session factory for the process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url_str,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            # Server-side prepared statements interact badly with connection
            # poolers; disabling the cache keeps asyncpg safe behind PgBouncer.
            connect_args={"statement_cache_size": 0},
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, rolling back on error and always closing."""
        async with self._sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside an explicit transaction.

        Use this for every multi-statement mutation, in particular the approval
        and execution paths, which rely on ``SELECT ... FOR UPDATE`` holding for
        the whole unit of work.
        """
        async with self._sessionmaker() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()


_database: Database | None = None


def get_database(settings: Settings | None = None) -> Database:
    global _database
    if _database is None:
        _database = Database(settings or get_settings())
    return _database


async def reset_database() -> None:
    """Dispose the process-wide database. Used by tests and shutdown."""
    global _database
    if _database is not None:
        await _database.dispose()
        _database = None
