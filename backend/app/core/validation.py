"""Reusable application-level validation helpers.

These validators enforce rules that cannot (or should not) be expressed as
DB constraints — typically because they depend on request context.
"""

from __future__ import annotations

from backend.app.core.errors import UnprocessableEntityError


def validate_required_reason(action: str, reason: str | None) -> None:
    """Raise UnprocessableEntityError if ``reason`` is absent or blank.

    Used for actions where an audit trail reason is mandatory (reject,
    request_changes). First consumer: Phase-4 Approval module.

    Args:
        action: Human-readable action name for the error message (e.g. "reject").
        reason: The reason string supplied by the caller (may be None or "").
    """
    if not reason or not reason.strip():
        raise UnprocessableEntityError(f"A non-empty reason is required for the '{action}' action.")
