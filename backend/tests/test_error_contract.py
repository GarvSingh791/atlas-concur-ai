"""Validate gate (part 1): a forced error returns the standard envelope with a
correlation ID.

Hermetic — no DB. Builds a throwaway app wired EXACTLY like ``create_app``
(the CorrelationIDMiddleware + all four exception handlers) with routes that
deliberately fail, so we exercise the REAL production handlers. The throwaway
app lives only in this module — it never leaks into ``database/models`` or the
real router tree.

``raise_server_exceptions=False`` is required so the 500 path returns the
rendered envelope instead of re-raising the exception into the test.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.errors import (
    AppError,
    ErrorCode,
    NotFoundError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from backend.app.main_entry.app import app as real_app

_SECRET_MARKER = "kaboom-do-not-leak-this-trace"


class _Body(BaseModel):
    name: str


def _build_app() -> FastAPI:
    """A minimal app mirroring create_app's error wiring, plus failing routes."""
    application = FastAPI()
    application.add_middleware(CorrelationIDMiddleware)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    # Handlers narrowed to a specific exception subtype are runtime-correct (this
    # is exactly how create_app registers them) but trip the Starlette stub's
    # arg-type, which expects Callable[[Request, Exception], ...]. Same known
    # false-positive the source scopes via tool.mypy.overrides for main_entry.app.
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]

    @application.post("/boom/validation")
    async def _validation(body: _Body) -> dict[str, str]:  # pragma: no cover - body
        return {"name": body.name}

    @application.get("/boom/http")
    async def _http() -> None:
        raise HTTPException(status_code=404, detail="thing not found")

    @application.get("/boom/app")
    async def _app_error() -> None:
        raise NotFoundError("entity missing")

    @application.get("/boom/unhandled")
    async def _unhandled() -> None:
        raise RuntimeError(_SECRET_MARKER)

    @application.get("/ok")
    async def _ok() -> dict[str, bool]:
        return {"ok": True}

    return application


@pytest.fixture(scope="module")
def client() -> TestClient:
    # raise_server_exceptions=False -> the 500 handler's response is returned.
    return TestClient(_build_app(), raise_server_exceptions=False)


def _assert_envelope(body: dict, expected_code: str) -> str:
    """Assert the standard envelope shape; return the correlation_id."""
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert set(error.keys()) == {"code", "message", "correlation_id"}
    assert error["code"] == expected_code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["correlation_id"], str) and error["correlation_id"]
    return error["correlation_id"]


# ---- Each error class maps to the same envelope -----------------------------


def test_validation_error_envelope(client: TestClient) -> None:
    resp = client.post("/boom/validation", json={})
    assert resp.status_code == 422
    cid = _assert_envelope(resp.json(), ErrorCode.VALIDATION)
    assert resp.headers["X-Correlation-ID"] == cid


def test_http_exception_envelope(client: TestClient) -> None:
    resp = client.get("/boom/http")
    assert resp.status_code == 404
    cid = _assert_envelope(resp.json(), ErrorCode.NOT_FOUND)
    assert resp.headers["X-Correlation-ID"] == cid


def test_app_error_envelope(client: TestClient) -> None:
    resp = client.get("/boom/app")
    assert resp.status_code == 404
    cid = _assert_envelope(resp.json(), ErrorCode.NOT_FOUND)
    assert resp.json()["error"]["message"] == "entity missing"
    assert resp.headers["X-Correlation-ID"] == cid


def test_unhandled_exception_envelope_and_no_leak(client: TestClient) -> None:
    resp = client.get("/boom/unhandled")
    assert resp.status_code == 500
    _assert_envelope(resp.json(), ErrorCode.INTERNAL)

    # The 500 body must NOT leak the exception detail or a traceback.
    raw = resp.text
    assert _SECRET_MARKER not in raw
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw


# ---- Correlation-ID propagation ---------------------------------------------


def test_inbound_x_request_id_is_honored(client: TestClient) -> None:
    fixed = "req-" + uuid.uuid4().hex
    resp = client.get("/boom/app", headers={"X-Request-ID": fixed})
    assert resp.headers["X-Correlation-ID"] == fixed
    assert resp.json()["error"]["correlation_id"] == fixed


def test_inbound_x_correlation_id_is_honored(client: TestClient) -> None:
    fixed = "cid-" + uuid.uuid4().hex
    resp = client.get("/ok", headers={"X-Correlation-ID": fixed})
    assert resp.status_code == 200
    assert resp.headers["X-Correlation-ID"] == fixed


def test_x_correlation_id_wins_over_x_request_id(client: TestClient) -> None:
    correlation = "cid-" + uuid.uuid4().hex
    request_id = "req-" + uuid.uuid4().hex
    resp = client.get(
        "/ok",
        headers={"X-Correlation-ID": correlation, "X-Request-ID": request_id},
    )
    assert resp.headers["X-Correlation-ID"] == correlation


def test_correlation_id_generated_when_absent(client: TestClient) -> None:
    resp = client.get("/ok")
    assert resp.status_code == 200
    generated = resp.headers["X-Correlation-ID"]
    assert generated
    # A generated id is a parseable uuid4.
    uuid.UUID(generated)


# ---- Real app sanity: unknown path still uses the envelope ------------------


def test_real_app_unknown_path_uses_envelope() -> None:
    # No `with` -> lifespan/DB connectivity check is not triggered (hermetic).
    client = TestClient(real_app)
    resp = client.get("/no/such/route")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), ErrorCode.NOT_FOUND)


def test_real_app_health_still_ok() -> None:
    client = TestClient(real_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
