"""C7-24 regression: 405 (and other unmapped 4xx) envelope code + preserved headers.

Staging repro: ``GET /api/v1/receipts`` and ``POST/PATCH/PUT/DELETE /api/v1/audit``
returned 405 with ``code: INTERNAL_SERVER_ERROR`` and no ``Allow`` header, because
``http_exception_handler`` mapped only 400/401/403/404 and ``_envelope`` dropped
``exc.headers``.

Hermetic — no DB (the real app is driven without its lifespan).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.errors import ErrorCode, http_exception_handler
from backend.app.main_entry.app import app as real_app


def _assert_envelope(resp, code: str) -> None:  # type: ignore[no-untyped-def]
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["correlation_id"] == resp.headers["X-Correlation-ID"]


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        # UAT2 added GET /receipts (attachable list); PUT is still unmapped.
        ("PUT", "/api/v1/receipts", {"POST"}),
        ("POST", "/api/v1/audit", {"GET"}),
        ("PATCH", "/api/v1/audit", {"GET"}),
        ("PUT", "/api/v1/audit", {"GET"}),
        ("DELETE", "/api/v1/audit", {"GET"}),
    ],
)
def test_real_app_405_has_method_not_allowed_code_and_allow_header(
    method: str, path: str, allowed: set[str]
) -> None:
    client = TestClient(real_app)  # no `with` → lifespan / DB not triggered
    resp = client.request(method, path)
    assert resp.status_code == 405
    _assert_envelope(resp, ErrorCode.METHOD_NOT_ALLOWED)
    assert resp.json()["error"]["code"] != ErrorCode.INTERNAL
    allow = {m.strip() for m in resp.headers["Allow"].split(",")}
    assert allowed <= allow


def _app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(CorrelationIDMiddleware)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    @application.get("/c/{status}")
    async def _raise(status: int) -> None:
        headers = {"Retry-After": "7"} if status == 429 else None
        if status == 401:
            headers = {"WWW-Authenticate": "Bearer"}
        raise HTTPException(status_code=status, detail="x", headers=headers)

    @application.get("/spoof")
    async def _spoof() -> None:
        raise HTTPException(status_code=409, detail="x", headers={"X-Correlation-ID": "evil"})

    return application


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.BAD_REQUEST),
        (401, ErrorCode.UNAUTHORIZED),
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (406, ErrorCode.NOT_ACCEPTABLE),
        (409, ErrorCode.CONFLICT),
        (413, ErrorCode.PAYLOAD_TOO_LARGE),
        (415, ErrorCode.UNSUPPORTED_MEDIA_TYPE),
        (422, ErrorCode.VALIDATION),
        (429, ErrorCode.TOO_MANY_REQUESTS),
        (418, ErrorCode.BAD_REQUEST),  # any other 4xx → client error, never INTERNAL
        (503, ErrorCode.INTERNAL),
    ],
)
def test_http_exception_status_code_mapping(status: int, code: str) -> None:
    resp = TestClient(_app()).get(f"/c/{status}")
    assert resp.status_code == status
    _assert_envelope(resp, code)


def test_exception_headers_are_preserved() -> None:
    client = TestClient(_app())
    assert client.get("/c/429").headers["Retry-After"] == "7"
    assert client.get("/c/401").headers["WWW-Authenticate"] == "Bearer"


def test_exception_headers_cannot_override_correlation_id() -> None:
    resp = TestClient(_app()).get("/spoof", headers={"X-Correlation-ID": "cid-real"})
    assert resp.headers["X-Correlation-ID"] == "cid-real"
    assert resp.json()["error"]["correlation_id"] == "cid-real"
