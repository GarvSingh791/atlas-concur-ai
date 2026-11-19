"""Pytest configuration for services/tests.

Sets minimal env vars needed to instantiate Settings in isolation.
JWT_SECRET and DATABASE_URL are required by Settings at instantiation time.

Provider selectors are forced to "local" so that a developer's .env file
(which may set LLM_PROVIDER=anthropic etc. for manual testing) does not
cause hermetic CI tests to fail. os.environ takes priority over .env in
pydantic_settings, so these setdefault calls win over any .env value.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-secret-for-services-ci")
os.environ.setdefault("DATABASE_URL", "postgresql://atlas_user:test@localhost:5432/atlas_db")

# Force local adapters in test — prevents a developer's .env (e.g.
# LLM_PROVIDER=anthropic) from breaking hermetic provider factory tests.
os.environ.setdefault("LLM_PROVIDER", "local")
os.environ.setdefault("EMBEDDINGS_PROVIDER", "local")
os.environ.setdefault("OCR_PROVIDER", "local")
os.environ.setdefault("STORAGE_PROVIDER", "local")
os.environ.setdefault("RERANK_PROVIDER", "local")
