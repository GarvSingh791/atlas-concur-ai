"""Alembic env.py — Atlas migration runner.

Reads the database URL from Settings (never hardcoded in alembic.ini).
Imports all models so Base.metadata is fully populated before autogenerate
or upgrade/downgrade are run.

Run from the repo root:
  alembic upgrade head
  alembic downgrade base
  alembic revision --autogenerate -m "description"
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL, make_url

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so all package imports resolve.
# alembic.ini sets prepend_sys_path = . but we also guard here.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Import every model so Base.metadata is fully populated.
# The order matters for FK resolution but SQLAlchemy sorts DDL internally.
# ---------------------------------------------------------------------------
import database.models  # noqa: F401, E402 — side-effect import registers all models

from database.models.base import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Settings — read the DATABASE_URL from the environment (Dev Rule 12).
# ---------------------------------------------------------------------------
from config.settings import get_settings  # noqa: E402

# Alembic config object
config = context.config

# Interpret the config file for logging.  disable_existing_loggers=False: when
# migrations run in-process (test fixtures, scripts) the default would silently
# disable every logger the app had already created, hiding their warnings.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The metadata object that holds all table definitions
target_metadata = Base.metadata


def _get_url() -> URL:
    """Return a psycopg2-normalised URL object from Settings.

    Important: return the URL *object*, not str(url_object).
    str() hides the password as '***', which causes authentication to fail.
    """
    raw = get_settings().DATABASE_URL
    return make_url(raw).set(drivername="postgresql+psycopg2")


def run_migrations_offline() -> None:
    """Run migrations without a live connection.

    Emits SQL to stdout so it can be reviewed or piped to psql.
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection (the default)."""
    url = _get_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
