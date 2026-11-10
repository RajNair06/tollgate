"""Alembic environment: migrates gateway.models.Base.metadata."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import dburl
from gateway.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL wins; fall back to the local dev default (same as config.py).
# Normalized through dburl — exactly like config.get_settings() — so hosted
# URLs work unchanged: bare postgres:// / postgresql:// become the psycopg
# dialect, and Supabase hosts get sslmode=require. Render's build command
# runs `alembic upgrade head` against the production DATABASE_URL, which is a
# plain postgresql:// pooler string; without this the migration would pick
# the wrong driver and connect without TLS.
config.set_main_option(
    "sqlalchemy.url",
    dburl.for_sqlalchemy(os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://tollgate:tollgate@localhost:5433/tollgate",
    ).strip()),
)

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
