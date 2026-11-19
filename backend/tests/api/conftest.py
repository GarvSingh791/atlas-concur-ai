"""Additive fixtures for the P7-T2 API contract matrix.

These COMPOSE the shared backend/tests/conftest.py fixtures (db_session, app,
role clients, factories) — none are redefined here. They add only what the
contract matrix needs on top: an unauthenticated client, a role->client map,
and deterministic local-provider wiring for the receipt + AI paths.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.app.modules.ai import runtime
from database.seeds.identity_seed import seed_rbac_catalog
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.local import LocalEmbeddingsProvider
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.local import LocalStorageProvider

# All DB-backed; self-skip when Postgres is unreachable (mirrors the suite).
pytestmark = pytest.mark.db


@pytest.fixture(scope="session", autouse=True)
def _persistent_rbac_catalog(_schema):
    """Seed the RBAC catalog ONCE (committed) for this package's session.

    ADDITIVE — does not redefine the shared function-scoped ``rbac_catalog``
    fixture. It works around a PRE-EXISTING isolation flake in the shared test
    foundation: ``seed_rbac_catalog()`` commits under
    ``join_transaction_mode="create_savepoint"``, which intermittently leaves a
    connection holding the ``uq_roles_key`` lock, so a later test's get-or-create
    collides with ``psycopg2.errors.UniqueViolation`` at SETUP (reproducible with
    the existing test_rbac.py / test_record_scoping.py suites, live-Postgres only;
    hermetic CI skips ``db`` tests). Pre-seeding the catalog permanently makes
    every per-test ``seed_rbac_catalog()`` a pure get-or-create NO-OP (no INSERT,
    no unique-key contention), so this package's matrix is deterministic.

    This is a TEST-LAYER mitigation, not a fix of the foundation defect — the
    defect is FLAGGED for Garv in the FSR. ``seed_rbac_catalog`` seeds only
    roles/permissions/role_permissions (no orgs/users), so permanent seeding does
    not introduce demo rows that could perturb other tests.
    """
    if _schema is None:  # Postgres unreachable — DB tests self-skip elsewhere.
        yield
        return
    with Session(_schema) as session:
        seed_rbac_catalog(session)
        session.commit()
    yield


@pytest.fixture()
def anon_client(app: Any) -> TestClient:
    """A TestClient with NO Authorization header (for 401 probes)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def role_clients(
    employee_client: TestClient,
    manager_client: TestClient,
    finance_client: TestClient,
) -> dict[str, TestClient]:
    """Map role-key -> org-A real-JWT client. Keys match endpoints.py role ids."""
    return {
        "employee": employee_client,
        "manager": manager_client,
        "finance_admin": finance_client,
    }


@pytest.fixture()
def local_providers(app: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force deterministic Local providers for storage / OCR / LLM / embeddings.

    Covers the receipt-upload pipeline (storage + OCR + LLM gap-fill) and the AI
    orchestrator path (LLM + embeddings + the build_orchestrator direct-call
    seam), so streaming + idempotency tests are fully deterministic.
    """
    app.dependency_overrides[get_storage_provider] = lambda: LocalStorageProvider()
    app.dependency_overrides[get_ocr_provider] = lambda: LocalOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    app.dependency_overrides[get_embeddings_provider] = lambda: LocalEmbeddingsProvider()

    monkeypatch.setattr(runtime, "get_llm_provider", lambda: LocalLlmProvider())
    runtime.build_orchestrator.cache_clear()

    yield

    runtime.build_orchestrator.cache_clear()
    # app fixture clears dependency_overrides in its own teardown.
