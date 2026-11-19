"""PII / secret minimizer for JSONB summary fields (NFR-SEC5).

Applied by write_decision_log to input_summary, tools_used, and output_summary
before any persistence.  Pure function — no I/O, no logging, no exceptions
except for the public minimize() entry point which always returns a value.

Two-pass strategy:
  1. Key-based redaction: dict keys whose names suggest secrets/PII have their
     values unconditionally replaced with "[REDACTED]".
  2. Value-based masking: string values are scanned for recognizable PII/secret
     patterns (bearer tokens, e-mail addresses, credit-card-like digit runs)
     and those substrings are replaced.

Messages in GuardrailError raised elsewhere must never include un-minimized data.
"""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

# Keys whose values should always be redacted (case-insensitive substring match).
_SENSITIVE_KEY_RE = re.compile(
    r"password|secret|token|api[_\-]?key|authorization|auth_key|ssn|credit[_\-]?card|card[_\-]?num",
    re.IGNORECASE,
)

# Value patterns that indicate PII or secrets in string values.
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Credit card numbers: 16 digits optionally grouped by spaces/hyphens.
_CARD_RE = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")


def minimize(obj: Any) -> Any:
    """Return a copy of *obj* with secrets and PII redacted.

    Handles nested dicts and lists recursively.  Non-dict/list/str values pass
    through unchanged (int, float, bool, None, UUID, etc.).
    """
    if isinstance(obj, dict):
        return {
            k: _REDACTED if _SENSITIVE_KEY_RE.search(str(k)) else minimize(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [minimize(item) for item in obj]
    if isinstance(obj, str):
        return _mask_string(obj)
    return obj


def _mask_string(value: str) -> str:
    """Mask recognizable PII/secret patterns within a string value."""
    value = _BEARER_RE.sub(_REDACTED, value)
    value = _EMAIL_RE.sub(_REDACTED, value)
    value = _CARD_RE.sub(_REDACTED, value)
    return value
