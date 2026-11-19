"""Approval routing: threshold constant and binary auto-approve gate (P4-T12).

Decision A (approved): LOW_RISK_AUTO_APPROVE_MAX_USD = 500.00

Gate (all conditions must hold for auto_approve; any failure → escalate):
  0. is_policy_compliant is True — an EXPLICIT verdict from the fresh
     approval-time policy evaluation (C7-05).  None/unknown → escalate.
  1. subject_in_policy is True
  2. blocking_flag_count == 0
  3. requires_approver is False
  4. subject_total is not None  (unknown cost → escalate; conservative)
  5. subject_total ≤ LOW_RISK_AUTO_APPROVE_MAX_USD
  6. APPROVAL_DECIDE permission present  (checked upstream by agent.py; not re-checked here)

This module is pure logic — no session, no imports from backend/.
LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

from typing import Literal

from ai_agents.agents.approval_agent.evaluate import RiskAssessment

LOW_RISK_AUTO_APPROVE_MAX_USD: float = 500.00
"""Monetary ceiling for the auto-approve path.

Conservative by design; easily tunable.  No FX conversion — seed data is USD.
"""


def decide_route(assessment: RiskAssessment) -> Literal["auto_approve", "escalate"]:
    """Return 'auto_approve' or 'escalate' based on deterministic risk signals.

    Binary gate — no third path.  Any gate failure → escalate.
    """
    if (
        assessment.is_policy_compliant is True
        and assessment.subject_in_policy is True
        and assessment.blocking_flag_count == 0
        and not assessment.requires_approver
        and assessment.subject_total is not None
        and float(assessment.subject_total) <= LOW_RISK_AUTO_APPROVE_MAX_USD
    ):
        return "auto_approve"
    return "escalate"
