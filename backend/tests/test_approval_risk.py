"""Unit tests for server-side risk-level derivation (P5-T8).

``derive_risk_level`` maps the Approval Agent's deterministic ``risk_summary``
signals (plus ``is_auto_approved``) onto a Low/Med/High display level on the
SERVER, so the client never scores risk (renders server truth only). Pure
function — no DB, no marker.
"""

from __future__ import annotations

from backend.app.modules.approval.risk import derive_risk_level


def test_auto_approved_flag_is_low() -> None:
    assert derive_risk_level(risk_summary=None, is_auto_approved=True) == "low"


def test_auto_approve_route_is_low() -> None:
    rs = {"route": "auto_approve", "subject_in_policy": True, "blocking_flag_count": 0}
    assert derive_risk_level(risk_summary=rs, is_auto_approved=False) == "low"


def test_blocking_flag_is_high() -> None:
    rs = {"route": "escalate", "subject_in_policy": False, "blocking_flag_count": 2}
    assert derive_risk_level(risk_summary=rs, is_auto_approved=False) == "high"


def test_escalated_in_policy_no_blocking_is_medium() -> None:
    # Escalated for a soft reason (e.g. over the amount threshold) — review, not a violation.
    rs = {"route": "escalate", "subject_in_policy": True, "blocking_flag_count": 0}
    assert derive_risk_level(risk_summary=rs, is_auto_approved=False) == "medium"


def test_escalated_out_of_policy_no_blocking_is_medium() -> None:
    rs = {"route": "escalate", "subject_in_policy": False, "blocking_flag_count": 0}
    assert derive_risk_level(risk_summary=rs, is_auto_approved=False) == "medium"


def test_blocking_count_missing_treated_as_zero() -> None:
    rs = {"route": "escalate", "subject_in_policy": False}
    assert derive_risk_level(risk_summary=rs, is_auto_approved=False) == "medium"


def test_null_risk_summary_not_auto_defaults_low() -> None:
    # Legacy/seed approval with no risk_summary and not auto-approved: no signals → low.
    assert derive_risk_level(risk_summary=None, is_auto_approved=False) == "low"
