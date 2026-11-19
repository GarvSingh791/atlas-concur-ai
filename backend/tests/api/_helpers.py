"""Shared assertion + request helpers for the API contract matrix.

The single ``assert_error_envelope`` is the central uniformity check: EVERY
error path in the suite routes through it, so the envelope shape is proven once
and applied everywhere.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from httpx import Response

# The nested envelope shape (authoritative — docs/phase_logs/P3-T4):
#   {"error": {"code": str, "message": str, "correlation_id": str}}
_ENVELOPE_TOP_KEYS = {"error"}
_ENVELOPE_INNER_KEYS = {"code", "message", "correlation_id"}

CORRELATION_HEADER = "X-Correlation-ID"


def assert_error_envelope(resp: Response, expected_status: int | None = None) -> str:
    """Assert the standard nested error envelope + correlation header.

    Returns the correlation_id. This is the ONE shape check the whole matrix
    funnels through, so envelope uniformity is asserted centrally.
    """
    if expected_status is not None:
        assert (
            resp.status_code == expected_status
        ), f"expected {expected_status}, got {resp.status_code}: {resp.text[:300]}"
    # Must never be a 5xx for the contract paths we exercise here.
    body = resp.json()
    assert set(body.keys()) == _ENVELOPE_TOP_KEYS, f"top-level keys: {body.keys()}"
    error = body["error"]
    assert set(error.keys()) == _ENVELOPE_INNER_KEYS, f"inner keys: {error.keys()}"
    assert isinstance(error["code"], str) and error["code"], "code must be a non-empty str"
    assert isinstance(error["message"], str) and error["message"], "message must be non-empty"
    cid = error["correlation_id"]
    assert isinstance(cid, str) and cid, "correlation_id must be present"
    # Header echoes the same id on every error response.
    assert (
        resp.headers.get(CORRELATION_HEADER) == cid
    ), f"header {CORRELATION_HEADER}={resp.headers.get(CORRELATION_HEADER)!r} != body cid {cid!r}"
    return cid


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE stream body into [(event_name, data_dict), ...]."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data = "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        events.append((name, json.loads(data)))
    return events


def sse_done_message_id(text: str) -> str | None:
    """Return the message_id carried by the SSE 'done' frame, if any."""
    for name, data in parse_sse(text):
        if name == "done":
            return data.get("message_id")
    return None


def dummy_uuid() -> str:
    """A fresh random UUID string for path params that must parse but not exist."""
    return str(uuid.uuid4())
