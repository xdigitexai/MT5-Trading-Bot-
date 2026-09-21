"""Alembic environment.

The README documents `alembic upgrade head`, but this module was never committed, so the command
failed with "Can't find Python file migrations/env.py" and the schema could not be created.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.database.base import Base

config = context.config

# alembic.ini ships without logger sections, which fileConfig would reject.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # pragma: no cover - logging setup must never block a migration
        pass

# The application URL is the single source of truth, so a deployment never migrates the wrong database.
config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
