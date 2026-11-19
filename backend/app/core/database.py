"""Shared database engine, session factory, and the FastAPI session dependency.

This is the single source of the SQLAlchemy engine for the whole app. Both the
startup connectivity check (lifespan) and request handlers go through here, so
there is ONE connection path — never two parallel engines.

Sync SQLAlchemy 2.0: the project ships ``psycopg2-binary`` (a sync DBAPI), so the
engine and sessions are sync. The URL is read exclusively from ``Settings`` — no
hardcoded URL or credentials.

The engine and session factory are built lazily behind ``lru_cache`` (mirroring
``get_settings()``), so importing this module has no side effects and opens no
connection — which keeps the hermetic smoke test green.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Build (once) and return the shared sync engine from ``Settings``.

    The DSN is normalized to the ``postgresql+psycopg2`` driver: the runtime
    ``.env`` / docker-compose use the bare ``postgresql://`` form while
    conftest/CI use ``postgresql+psycopg2://`` — pinning the driver keeps the
    sync psycopg2 path consistent regardless of which form is configured.
    """
    url = make_url(get_settings().DATABASE_URL).set(drivername="postgresql+psycopg2")
    return create_engine(url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Build (once) and return the session factory bound to the shared engine."""
    # No `autocommit=` — it was removed in SQLAlchemy 2.0 and passing it raises.
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and guarantee cleanup.

    Rolls back on any exception raised by the handler and always closes the
    session, so no connection is leaked back to the pool in a dirty state.
    Committing successful work is the caller/service layer's responsibility.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
