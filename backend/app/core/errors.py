"""
Standard error envelope for Atlas.

Every error response has the shape:
    {
      "error": {
        "code": "<error_code>",
        "message": "<human-readable generic message>",
        "correlation_id": "<uuid>"
      }
    }

Full exception details are logged server-side only — never returned to the
client.  This prevents information leakage while keeping structured logs
searchable by correlation_id.

Design notes
------------
* Correlation ID is read from request.scope["_correlation_id"] (set by
  CorrelationIDMiddleware before the request enters the app and durable for
  the full request lifetime, including ServerErrorMiddleware-level handlers
  that run after the ContextVar token is reset).

* The X-Correlation-ID header is embedded directly in every JSONResponse
  object returned by these handlers.  This guarantees the header appears on
  ALL error responses regardless of which send() path is used — in particular,
  when an unhandled exception propagates past our ASGI send_with_header wrapper
  and is caught by ServerErrorMiddleware, our wrapper is no longer in the send
  chain, so header injection must happen inside the response object itself.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from backend.app.core.correlation import (
    CORRELATION_ID_HEADER,
    get_correlation_id,
    get_correlation_id_from_request,
)

logger = logging.getLogger(__name__)

# Path segments that carry a one-time secret (phase 12 follow-ups: the invite
# peek GET /auth/invite/{token}).  Logged paths are redacted so a log reader can
# never pick up a live invitation.
_SECRET_PATH_SEGMENT = re.compile(r"(/auth/invite/)[^/?#]+")


def loggable_path(path: object) -> str:
    """``path`` with any secret-bearing segment replaced by ``[redacted]``."""
    return _SECRET_PATH_SEGMENT.sub(r"\1[redacted]", str(path))


# ---- Pydantic models for the envelope ---------------------------------------


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


# ---- Error codes ------------------------------------------------------------


class ErrorCode:
    INTERNAL = "INTERNAL_SERVER_ERROR"
    VALIDATION = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    BAD_REQUEST = "BAD_REQUEST"
    CONFLICT = "CONFLICT"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    NOT_ACCEPTABLE = "NOT_ACCEPTABLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    RATE_LIMITED = "RATE_LIMITED"
    # The shared rate-limit store is unreachable and RATE_LIMIT_REDIS_FAILURE=closed.
    RATE_LIMIT_UNAVAILABLE = "RATE_LIMIT_UNAVAILABLE"


# ---- Application exception types --------------------------------------------
#
# The single, reusable exception hierarchy domain code raises (Dev Rule 5:
# shared contracts defined once). Each carries an HTTP status, a machine-readable
# code, and a SAFE, client-facing message — so app_error_handler can render the
# standard envelope without any handler-specific logic. Subclasses cover the
# 4xx cases mirrored in http_exception_handler's code_map; add more as needed.


class AppError(Exception):
    """Base application error: HTTP status + machine code + safe message.

    ``message`` is returned to the client verbatim, so it MUST NOT contain
    secrets, PII, or internal detail. The default is a 500 with the generic
    INTERNAL code; subclasses set their own status/code.
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL
    # Optional protocol headers (e.g. Retry-After) and extra, SAFE envelope
    # fields (e.g. retry_after_seconds) rendered by app_error_handler.
    headers: Mapping[str, str] | None = None
    extra: Mapping[str, object] | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class BadRequestError(AppError):
    status_code = 400
    code = ErrorCode.BAD_REQUEST


class UnauthorizedError(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHORIZED


class AmbiguousCredentialsError(UnauthorizedError):
    """The SAME generic 401 as a wrong password, raised when a no-organization
    login cannot pick one account (more active same-email accounts than the
    candidate cap, or a password matching several).  The client sees nothing
    different; the login guard does not count it against the email's signal.
    """


class ForbiddenError(AppError):
    status_code = 403
    code = ErrorCode.FORBIDDEN


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT


class PreconditionRequiredError(AppError):
    """428: a write that must carry a precondition (If-Match / Idempotency-Key) did not."""

    status_code = 428
    code = ErrorCode.BAD_REQUEST


class UnprocessableEntityError(AppError):
    """422 for validation failures that aren't Pydantic schema errors."""

    status_code = 422
    code = ErrorCode.VALIDATION


# ---- Helper -----------------------------------------------------------------


def _get_cid(request: Request) -> str:
    """Correlation ID — scope first (always available), then ContextVar fallback."""
    return get_correlation_id_from_request(request) or get_correlation_id()


def _envelope(
    code: str,
    message: str,
    status: int,
    request: Request,
    extra_headers: Mapping[str, str] | None = None,
    extra_fields: Mapping[str, object] | None = None,
) -> JSONResponse:
    cid = _get_cid(request)
    # Protocol headers carried by the exception (e.g. ``Allow`` on a 405,
    # ``Retry-After`` on a 429, ``WWW-Authenticate`` on a 401) are preserved;
    # the correlation-ID header always wins over any same-named extra header.
    headers: dict[str, str] = dict(extra_headers or {})
    headers[CORRELATION_ID_HEADER] = cid
    # Extra fields never override the three envelope keys.
    error: dict[str, object] = {"code": code, "message": message, "correlation_id": cid}
    for key, value in (extra_fields or {}).items():
        error.setdefault(key, value)
    return JSONResponse(status_code=status, headers=headers, content={"error": error})


# ---- Exception handlers (registered in app factory) ------------------------


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions.

    Logs the full traceback server-side; returns a generic 500 to the client.
    Stack traces are NEVER included in the response body (security rule).
    """
    cid = _get_cid(request)
    logger.error(
        "Unhandled exception [%s] %s %s",
        cid,
        request.method,
        loggable_path(request.url.path),
        exc_info=exc,
    )
    return _envelope(ErrorCode.INTERNAL, "An unexpected error occurred.", 500, request)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code_map = {
        400: ErrorCode.BAD_REQUEST,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        405: ErrorCode.METHOD_NOT_ALLOWED,
        406: ErrorCode.NOT_ACCEPTABLE,
        409: ErrorCode.CONFLICT,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
        422: ErrorCode.VALIDATION,
        429: ErrorCode.TOO_MANY_REQUESTS,
    }
    # Any other 4xx is a client error, never an INTERNAL_SERVER_ERROR (C7-24);
    # only genuine 5xx statuses fall back to the INTERNAL code.
    fallback = ErrorCode.BAD_REQUEST if 400 <= exc.status_code < 500 else ErrorCode.INTERNAL
    code = code_map.get(exc.status_code, fallback)
    message = str(exc.detail) if exc.detail else "Request error."
    return _envelope(code, message, exc.status_code, request, exc.headers)


def _loggable_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """Validation errors without the submitted values.

    Pydantic puts the offending ``input`` (for a missing field: the whole request
    body) and ``ctx`` on each error; bodies carry passwords (POST /auth/login,
    POST /users), so only type/loc/msg are logged — never what the client sent.
    """
    return [{k: v for k, v in err.items() if k not in ("input", "ctx")} for err in exc.errors()]


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    cid = _get_cid(request)
    logger.warning(
        "Validation error [%s] %s %s: %s",
        cid,
        request.method,
        loggable_path(request.url.path),
        _loggable_errors(exc),
    )
    return _envelope(ErrorCode.VALIDATION, "Request validation failed.", 422, request)


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Map SQLAlchemy IntegrityError to standard envelope based on SQLSTATE pgcode.

    Only the safe generic message is returned to the client — no SQL detail,
    table names, or column names. The pgcode is logged server-side only.
    """
    pgcode: str | None = getattr(getattr(exc, "orig", None), "pgcode", None)
    logger.warning(
        "IntegrityError [%s] %s %s pgcode=%s",
        _get_cid(request),
        request.method,
        loggable_path(request.url.path),
        pgcode,
    )

    if pgcode == "23505":  # unique_violation
        status = 409
        code = ErrorCode.CONFLICT
        message = "A record with those values already exists."
    elif pgcode == "23514":  # check_violation
        status = 422
        code = ErrorCode.VALIDATION
        message = "A value failed a database constraint check."
    elif pgcode == "23502":  # not_null_violation
        status = 422
        code = ErrorCode.VALIDATION
        message = "A required value was not provided."
    elif pgcode == "23503":  # foreign_key_violation
        status = 409
        code = ErrorCode.CONFLICT
        message = "The referenced record does not exist."
    else:
        status = 409
        code = ErrorCode.CONFLICT
        message = "A database integrity constraint was violated."

    return _envelope(code, message, status, request)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Render any raised ``AppError`` into the standard envelope.

    5xx errors are logged with the full traceback (server-side only); 4xx errors
    are expected/operational and logged at WARNING. The client always receives
    only the safe ``message`` + code + correlation_id.
    """
    cid = _get_cid(request)
    if exc.status_code >= 500:
        logger.error(
            "Application error [%s] %s %s",
            cid,
            request.method,
            loggable_path(request.url.path),
            exc_info=exc,
        )
    else:
        logger.warning(
            "Application error [%s] %s %s: %s",
            cid,
            request.method,
            loggable_path(request.url.path),
            exc.code,
        )
    return _envelope(exc.code, exc.message, exc.status_code, request, exc.headers, exc.extra)
