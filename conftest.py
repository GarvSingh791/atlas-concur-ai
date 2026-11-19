"""Pytest session bootstrap (repo root).

config.settings instantiates the Settings singleton at IMPORT time
(config/settings/__init__.py) and calls sys.exit(1) if the two required
variables — JWT_SECRET and DATABASE_URL — are missing or empty. Any test that
imports the FastAPI app therefore needs those present before collection imports
the app module.

pytest imports the rootdir conftest.py before collecting test modules, so the
module-level setdefault() calls below run first. They are non-sensitive test
stubs and never override a real value (CI also sets these as job env; local
runs may have a real .env). The database is never actually contacted: the
smoke test drives TestClient WITHOUT the `with` context manager, so the
lifespan DB-connectivity check never runs.
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-a-real-key-do-not-use")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test",
)
# Pin deterministic providers for all tests so that invalid values in .env
# (e.g. LLM_PROVIDER=mock) are overridden — env vars take precedence over .env.
os.environ.setdefault("LLM_PROVIDER", "local")
os.environ.setdefault("EMBEDDINGS_PROVIDER", "local")
# Phase 11: the request limiter is ON by default in every deployed config; the
# suite makes thousands of logins / calls from one test client, so it is OFF
# here.  Rate-limit tests install their own enabled limiter on app.state.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# Phase 12: the scheduled demo reset is OFF in the suite (it is also never started
# implicitly under pytest); scheduler tests construct and start it explicitly.
os.environ.setdefault("DEMO_RESET_SCHEDULE", "off")
