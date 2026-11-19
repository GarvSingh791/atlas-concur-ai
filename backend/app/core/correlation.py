"""
Correlation-ID pure ASGI middleware.

Every inbound request gets a UUID correlation ID:
  - Read from the X-Correlation-ID request header if supplied by a caller.
  - Else read from the X-Request-ID header (the common gateway/proxy alias);
    X-Correlation-ID takes precedence when both are present.
  - Generated as a uuid4 if neither is present.

Regardless of which inbound header (if any) supplied it, the ID is always echoed
back on the canonical X-Correlation-ID response header.

The ID is stored in TWO complementary places:

  1. scope["_correlation_id"]   — a plain key on the ASGI scope dict.
     The scope dict is alive for the ENTIRE request lifecycle, even after
     exception propagation and outside the ContextVar's token window.
     This is what exception handlers (including ServerErrorMiddleware-level
     handlers) should read via request.scope["_correlation_id"].

  2. correlation_id_var ContextVar — available to log filters and helper
     code that runs WITHIN the middleware's try block (normal request path
     and inner-middleware exception handlers).  Reliable for logging;
     NOT reliable inside ServerErrorMiddleware-level handlers because the
     ContextVar token is reset in the finally block before ServerErrorMiddleware
     gets to invoke its handler.

Exception handlers should use get_correlation_id_from_scope(request.scope) /
get_correlation_id_from_request(request) rather than get_correlation_id() to
guarantee the ID is always present regardless of which handler layer fires.

The X-Correlation-ID response header is injected via a send() wrapper which
executes for ALL responses including error/500 responses.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_HEADER = "X-Correlation-ID"
CORRELATION_ID_HEADER_LOWER = CORRELATION_ID_HEADER.lower().encode()
# Common gateway/proxy alias; accepted inbound, but never echoed (X-Correlation-ID
# is the canonical response header).
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_HEADER_LOWER = REQUEST_ID_HEADER.lower().encode()
_SCOPE_KEY = "_correlation_id"

# ContextVar — reliable within the middleware try block and log filters
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the correlation ID from the ContextVar (current async context)."""
    return correlation_id_var.get()


def get_correlation_id_from_scope(scope: Scope) -> str:
    """Return the correlation ID from the ASGI scope (always safe, any layer)."""
    return scope.get(_SCOPE_KEY, "")


def get_correlation_id_from_request(request: Request) -> str:
    """Return the correlation ID from a Request object (always safe, any layer)."""
    return get_correlation_id_from_scope(request.scope)


class CorrelationIDMiddleware:
    """Pure ASGI middleware — attaches a correlation ID to every request cycle.

    Stores the ID in both the ASGI scope (durable for full request lifetime) and
    the ContextVar (available to log filters during normal request flow).

    Wraps send() to inject the X-Correlation-ID header on ALL responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # ---- Derive correlation ID ------------------------------------------
        # Honor X-Correlation-ID first, then X-Request-ID (alias). Scan once and
        # let X-Correlation-ID win if both headers are supplied.
        correlation_header: str = ""
        request_id_header: str = ""
        for key, value in scope.get("headers", []):
            if key == CORRELATION_ID_HEADER_LOWER:
                correlation_header = value.decode(errors="replace")
            elif key == REQUEST_ID_HEADER_LOWER:
                request_id_header = value.decode(errors="replace")
        raw = correlation_header or request_id_header
        cid = raw if raw else str(uuid.uuid4())

        # ---- Store in scope (durable across entire request + exception chain)
        scope[_SCOPE_KEY] = cid

        # ---- Store in ContextVar (for log filters, inner-scope helpers) -----
        token = correlation_id_var.set(cid)

        # ---- Wrap send to inject header on every response -------------------
        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # SET (not append): error handlers (errors._envelope) already
                # embed X-Correlation-ID in their JSONResponse, and those
                # responses travel back out through this wrapper. Appending would
                # emit the header twice; assigning keeps exactly one.
                headers[CORRELATION_ID_HEADER] = cid
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            correlation_id_var.reset(token)
