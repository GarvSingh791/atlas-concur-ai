"""Backend smoke tests — prove the pytest harness and FastAPI app wire up.

Hermetic by design: TestClient is constructed WITHOUT the `with` context
manager, so the lifespan handler (which performs a real database-connectivity
check) is never executed. These tests need no live Postgres. Real unit/API
tests arrive in Phase 7 (P7-T1/P7-T2) and extend this same runner.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.main_entry.app import app


def test_app_instance() -> None:
    """The module-level app is a configured FastAPI instance."""
    assert isinstance(app, FastAPI)


def test_health_endpoint() -> None:
    """GET /health returns 200 and the fixed liveness envelope — no DB needed."""
    client = TestClient(app)  # no `with` -> lifespan / DB check is NOT triggered
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {"status", "app", "env", "version"} <= body.keys()


def test_health_reports_commit_default_local() -> None:
    """/health carries the running release's commit; unset -> "local" (P8-T4).

    CI's deploy-staging job polls this field until it equals the pushed SHA, so
    the key must always be present and a string.
    """
    client = TestClient(app)
    body = client.get("/health").json()
    assert "commit" in body
    assert isinstance(body["commit"], str) and body["commit"]
