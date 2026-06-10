"""Alembic environment — async SQLAlchemy configuration."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _get_url() -> str:
    """Load DATABASE_URL from app settings (overrides alembic.ini placeholder)."""
    from app.config import get_settings

    return get_settings().database_url


def _get_target_metadata():
    """Import all models so Alembic can detect them via Base.metadata."""
    import app.models  # noqa: F401 — registers all ORM models
    from app.db import Base

    return Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection needed)."""
    context.configure(
        url=_get_url(),
        target_metadata=_get_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    """Execute migrations against a live connection."""
    context.configure(connection=connection, target_metadata=_get_target_metadata())
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations in 'online' mode."""
    engine = create_async_engine(_get_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Entry point for online mode — runs the async migration coroutine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
