"""Alembic environment (async).

The database URL comes from application Settings rather than alembic.ini, so
credentials exist in exactly one place.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings

# Importing the registry is what makes autogenerate see the tables.
from app.models import Base  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Keep autogenerate away from objects Alembic does not manage.

    From Phase 4 the snapshot tables are partitioned; their partitions are
    created by a maintenance job, not by migrations, and autogenerate would
    otherwise propose dropping them on every run.
    """
    if type_ == "table" and name.startswith(
        ("post_metric_snapshots_p", "account_metric_snapshots_p")
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
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
    # A caller may hand us a live connection through `config.attributes`, which
    # is how the Postgres test suite runs the real migrations against a database
    # it already owns. Without this branch env.py would always open its own
    # engine from settings, and the migrations could only ever be exercised
    # against whatever DATABASE_URL happened to be configured.
    injected = config.attributes.get("connection")
    if injected is not None:
        do_run_migrations(injected)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
