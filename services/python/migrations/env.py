import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Make the service package (``src``) importable regardless of the cwd alembic
# is invoked from. alembic.ini lives in ``services/python`` so its parent
# (this file's grandparent) is the service root that contains ``src``.
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.config import get_postgres_sync_url  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Inject the DB URL from code (assembled from POSTGRES_* env vars) instead of
# hardcoding a secret in alembic.ini. Runtime queries use asyncpg; only these
# migrations use the sync psycopg driver (postgresql+psycopg://...).
config.set_main_option("sqlalchemy.url", get_postgres_sync_url())

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = None

# The application tables live in a dedicated ``app`` schema (created by the
# first migration). The ``alembic_version`` bookkeeping table is intentionally
# kept in the default (``public``) schema: the first migration's downgrade
# drops the ``app`` schema with CASCADE, so co-locating the version table in
# ``app`` would let downgrade delete alembic's own bookkeeping mid-flight and
# break the down/up round-trip. Keeping it in ``public`` is the robust choice.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
