"""Alembic environment (async).

The URL comes from :mod:`stockbrain.config`, never from ``alembic.ini``, so a
migration can never run against a different database than the application.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from stockbrain.config import get_settings
from stockbrain.db.models import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is not a preference. `fileConfig`
    # defaults to True, which disables every logger that already exists and is
    # not named in `alembic.ini` -- and `alembic.ini` names only root,
    # sqlalchemy and alembic. Run in-process (a test suite, or any future
    # migrate-on-boot), that silences the entire `stockbrain.*` namespace for
    # the rest of the process: the application keeps working and stops saying
    # anything, which is the worst failure a logging change can cause.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_str)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_str,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
