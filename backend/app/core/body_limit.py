"""Request body size limit — pure ASGI middleware (phase 11, public-demo security).

Every HTTP request body is capped BEFORE a route sees it:

* ``Content-Length`` larger than the limit -> 413 at once; the body is never read.
* The streamed body is counted chunk by chunk as the app pulls it, so a missing
  or lying ``Content-Length`` (chunked transfer, a client that under-declares)
  cannot bypass the cap.  The moment the running total passes the limit the
  ``receive`` call raises ``HTTPException(413)``: at most the limit plus the one
  chunk that crossed it is ever handed to the app, so nothing upstream can
  buffer more than that.

The receipt upload route gets a larger limit (``MAX_UPLOAD_BYTES`` for the file
plus a small multipart allowance for the boundary and part headers); every
other route gets ``MAX_REQUEST_BODY_BYTES``.  Limits are read per request from
settings, so an operator change needs no code change.

The 413 uses the standard error envelope with code ``PAYLOAD_TOO_LARGE`` and the
request's correlation id.  The middleware is registered innermost (inside CORS
and the correlation-id middleware) so a browser can read the 413.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.correlation import CORRELATION_ID_HEADER, get_correlation_id_from_scope
from backend.app.core.errors import ErrorCode, loggable_path
from config.settings import get_settings

logger = logging.getLogger(__name__)

# Room for the multipart boundary, part headers and a client filename on top of
# the file itself.  The route re-checks the FILE against MAX_UPLOAD_BYTES exactly.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

_MIB = 1024 * 1024


def _mb(limit: int) -> str:
    value = limit / _MIB
    return f"{value:.0f} MB" if value >= 1 else f"{max(1, limit // 1024)} KB"


def upload_too_large_message(limit: int) -> str:
    return f"The receipt file is too large. The maximum size is {_mb(limit)}."


def body_too_large_message(limit: int) -> str:
    return f"The request body is too large. The maximum size is {_mb(limit)}."


class BodySizeLimitMiddleware:
    """Reject request bodies over the configured limit with a 413 envelope.

    ``upload_paths``: exact paths (trailing slash ignored) that accept a file
    upload and get ``upload_limit() + MULTIPART_OVERHEAD_BYTES``.
    ``default_limit`` / ``upload_limit``: callables so tests and operators can
    change the limits without rebuilding the app (defaults read settings).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        upload_paths: frozenset[str] = frozenset(),
        default_limit: Callable[[], int] | None = None,
        upload_limit: Callable[[], int] | None = None,
    ) -> None:
        self.app = app
        self.upload_paths = frozenset(p.rstrip("/") for p in upload_paths)
        self._default_limit = default_limit or (lambda: get_settings().MAX_REQUEST_BODY_BYTES)
        self._upload_limit = upload_limit or (lambda: get_settings().MAX_UPLOAD_BYTES)

    def _limits_for(self, scope: Scope) -> tuple[int, str]:
        """(byte limit, client message) for this request."""
        path = str(scope.get("path", "")).rstrip("/")
        if scope.get("method") == "POST" and path in self.upload_paths:
            file_limit = self._upload_limit()
            return file_limit + MULTIPART_OVERHEAD_BYTES, upload_too_large_message(file_limit)
        limit = self._default_limit()
        return limit, body_too_large_message(limit)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None  # unusable header: the streamed count still applies
        return None

    async def _reject(self, scope: Scope, send: Send, message: str) -> None:
        cid = get_correlation_id_from_scope(scope)
        response = JSONResponse(
            status_code=413,
            headers={CORRELATION_ID_HEADER: cid, "Connection": "close"},
            content={
                "error": {
                    "code": ErrorCode.PAYLOAD_TOO_LARGE,
                    "message": message,
                    "correlation_id": cid,
                }
            },
        )

        async def _no_body() -> Message:  # the body is deliberately never read
            return {"type": "http.disconnect"}

        await response(scope, _no_body, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit, message = self._limits_for(scope)
        declared = self._declared_length(scope)
        if declared is not None and declared > limit:
            logger.warning(
                "request body rejected before reading: declared=%d limit=%d path=%s",
                declared,
                limit,
                loggable_path(scope.get("path", "")),
            )
            await self._reject(scope, send, message)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                # Never pull more of an over-limit body, whatever the app does.
                raise HTTPException(status_code=413, detail=message)
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    exceeded = True
                    logger.warning(
                        "request body rejected while streaming: received>%d path=%s",
                        limit,
                        loggable_path(scope.get("path", "")),
                    )
                    raise HTTPException(status_code=413, detail=message)
            return msg

        async def tracking_send(msg: Message) -> None:
            nonlocal response_started
            if msg["type"] == "http.response.start":
                response_started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except Exception:
            # The app normally renders the HTTPException itself (413 envelope);
            # this covers code that swallowed it into another error type.
            if exceeded and not response_started:
                await self._reject(scope, send, message)
                return
            raise


class SecurityHeadersMiddleware:
    """Add ``X-Content-Type-Options: nosniff`` to every HTTP response.

    No API response may be content-sniffed into something executable by a
    browser (phase 11).  Pure ASGI; an existing value is overwritten.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in msg.get("headers", [])
                    if k.lower() != b"x-content-type-options"
                ]
                headers.append((b"x-content-type-options", b"nosniff"))
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, send_with_headers)
