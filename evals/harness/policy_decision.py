"""The M-A3 3-class mapping — ``classify(PolicyVerdict) -> str``.

This module turns the production ``PolicyEvalTool``'s *binary* per-item verdict
(``in_policy = all(v.in_policy)``) into the 3-class label the eval suite scores
against: ``in_policy`` | ``violation`` | ``needs_review``.

WHY a derived mapping (not the tool's bool)?
The tool answers "does every applicable rule pass?" — but two very different
situations both yield ``in_policy=True`` at the bare-bool level:

  1. Every applicable rule was actually evaluated and passed.            -> in_policy
  2. An applicable rule could NOT be evaluated (missing data) and the tool
     emits a *skip verdict* with ``in_policy=True, observed={}``
     [ai_agents/tools/policy_eval.py:623-633], OR no rule applied at all.
     A weak system would silently report (2) as "in_policy" — a SILENT PASS.

The eval treats case (2) as ``needs_review`` so an unevaluable item can never be
counted as a clean pass.  This is the operational definition the HARD GATE
(violation-class recall == 1.0) and the flag-precision floor are asserted on.

THE MAPPING (must match BUILD BRIEF section 4 verbatim)::

    def classify(verdict):
        failing = [v for v in verdict.verdicts if not v.in_policy]
        if failing:
            return "violation"          # any rule definitively failed
        if verdict.evaluated_rule_count == 0:
            return "needs_review"        # no applicable rule -> cannot assert in-policy
        if any(v.observed == {} for v in verdict.verdicts):
            return "needs_review"        # an applicable rule could not be evaluated (skip)
        return "in_policy"               # every applicable rule evaluated and passed

Gold labels in ``m_a3_policy.jsonl`` MUST be authored to match this definition.

Class constants are exported so the entrypoint and metrics layer share one
source of truth for label strings and ordering.
"""

from __future__ import annotations

# READ-ONLY import of the production verdict type. We never modify it.
from ai_agents.tools.policy_eval import PolicyVerdict

__all__ = [
    "IN_POLICY",
    "VIOLATION",
    "NEEDS_REVIEW",
    "LABELS",
    "FLAG_LABELS",
    "classify",
    "is_flag",
]

IN_POLICY = "in_policy"
VIOLATION = "violation"
NEEDS_REVIEW = "needs_review"

# Canonical label ordering for the 3x3 confusion matrix.
LABELS: tuple[str, str, str] = (IN_POLICY, VIOLATION, NEEDS_REVIEW)

# A "flag" is any predicted label that surfaces the item for human attention.
# Flag-class precision (>= floor) bounds how often the system cries wolf.
FLAG_LABELS: frozenset[str] = frozenset({VIOLATION, NEEDS_REVIEW})


def classify(verdict: PolicyVerdict) -> str:
    """Map a ``PolicyVerdict`` to its 3-class eval label.

    See the module docstring for the rationale. Pure; no I/O, no DB.
    """
    failing = [v for v in verdict.verdicts if not v.in_policy]
    if failing:
        return VIOLATION  # any rule definitively failed
    if verdict.evaluated_rule_count == 0:
        return NEEDS_REVIEW  # no applicable rule -> cannot assert in-policy
    if any(v.observed == {} for v in verdict.verdicts):
        return NEEDS_REVIEW  # an applicable rule could not be evaluated (skip verdict)
    return IN_POLICY  # every applicable rule evaluated and passed


def is_flag(label: str) -> bool:
    """True if ``label`` is a flag class (violation or needs_review)."""
    return label in FLAG_LABELS
