from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.errors import (
    AppError,
    ErrorCode,
    UnprocessableEntityError,
    app_error_handler,
    http_exception_handler,
    integrity_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from backend.app.core.validation import validate_required_reason

# ---------------------------------------------------------------------------
# Fake psycopg2-like "orig" object — avoids importing the real driver.
# ---------------------------------------------------------------------------


class _FakePgError(BaseException):
    """Mimics a psycopg2 DatabaseError with a pgcode attribute."""

    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


def _build_integrity_test_app() -> FastAPI:
    """Minimal FastAPI app wired with all production exception handlers."""
    application = FastAPI()
    application.add_middleware(CorrelationIDMiddleware)
    application.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]

    @application.get("/boom/unique")
    def boom_unique() -> None:
        raise IntegrityError("stmt", {}, _FakePgError("23505"))

    @application.get("/boom/check")
    def boom_check() -> None:
        raise IntegrityError("stmt", {}, _FakePgError("23514"))

    @application.get("/boom/fk")
    def boom_fk() -> None:
        raise IntegrityError("stmt", {}, _FakePgError("23503"))

    @application.get("/boom/unknown")
    def boom_unknown() -> None:
        raise IntegrityError("stmt", {}, _FakePgError("99999"))

    @application.get("/boom/no_pgcode")
    def boom_no_pgcode() -> None:
        raise IntegrityError("stmt", {}, None)  # type: ignore[arg-type]

    return application


class TestValidateRequiredReason:
    """Unit tests for the required-reason validator — no DB needed."""

    def test_none_reason_raises(self) -> None:
        with pytest.raises(UnprocessableEntityError) as exc_info:
            validate_required_reason("reject", None)
        assert "reject" in str(exc_info.value)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(UnprocessableEntityError):
            validate_required_reason("request_changes", "")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(UnprocessableEntityError):
            validate_required_reason("reject", "   ")

    def test_valid_reason_passes(self) -> None:
        validate_required_reason("reject", "Policy violation: over daily limit.")

    def test_valid_reason_with_leading_trailing_whitespace_passes(self) -> None:
        # strip() is only used to check emptiness; the value itself is returned unchanged
        validate_required_reason("reject", "  valid reason  ")

    def test_action_appears_in_error_message(self) -> None:
        with pytest.raises(UnprocessableEntityError) as exc_info:
            validate_required_reason("custom_action", None)
        assert "custom_action" in str(exc_info.value)

    def test_returns_none_on_success(self) -> None:
        validate_required_reason("reject", "Legit reason.")  # must not raise


# ---------------------------------------------------------------------------
# Hermetic IntegrityError mapping tests — no DB required.
# ---------------------------------------------------------------------------


class TestIntegrityErrorMapping:
    """Hermetic tests: IntegrityError pgcodes map to the correct envelope."""

    @pytest.fixture(scope="class")
    def client(self) -> TestClient:
        return TestClient(_build_integrity_test_app(), raise_server_exceptions=False)

    def test_unique_violation_returns_409_conflict(self, client: TestClient) -> None:
        resp = client.get("/boom/unique")
        assert resp.status_code == 409
        body = resp.json()
        assert set(body.keys()) == {"error"}
        error = body["error"]
        assert set(error.keys()) == {"code", "message", "correlation_id"}
        assert error["code"] == ErrorCode.CONFLICT
        cid = error["correlation_id"]
        assert isinstance(cid, str) and len(cid) > 0
        assert resp.headers.get("X-Correlation-ID") == cid

    def test_check_violation_returns_422_validation(self, client: TestClient) -> None:
        resp = client.get("/boom/check")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == ErrorCode.VALIDATION

    def test_fk_violation_returns_409_conflict(self, client: TestClient) -> None:
        resp = client.get("/boom/fk")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_unknown_pgcode_returns_409_not_500(self, client: TestClient) -> None:
        resp = client.get("/boom/unknown")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_no_orig_returns_409_not_500(self, client: TestClient) -> None:
        resp = client.get("/boom/no_pgcode")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_response_body_contains_no_sql_detail(self, client: TestClient) -> None:
        resp = client.get("/boom/unique")
        body_str = resp.text
        # No SQL statement text, internal table/column names, or driver names
        assert "stmt" not in body_str
        assert "psycopg" not in body_str.lower()
