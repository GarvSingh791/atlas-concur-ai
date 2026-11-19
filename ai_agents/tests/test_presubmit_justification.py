"""Hermetic tests — a written justification softens ONLY justifiable violations (C7R UAT).

``verdicts_to_flags`` is where pre-submit severity is decided.  A non-empty entry
justification makes a manager-approvable policy_violation of a
JUSTIFIABLE_RULE_TYPES rule (amount cap, per diem, verified preferred supplier)
advisory — the flag is kept, with the justification, for the approver — while
hard blocks stay blocking: missing receipt and an unverified supplier (evidence,
not prose), category_allow (never reimbursable), cabin class / any rule needing a
VP (no VP approval tier exists) and any unknown rule type (fail closed).
Approval-time ``_is_blocking`` still counts a justified violation as an
exception, so it can never be auto-approved.
"""

from __future__ import annotations

import dataclasses

import pytest

from ai_agents.agents.approval_agent.evaluate import _is_blocking
from ai_agents.agents.expense_agent.presubmit import (
    JUSTIFIABLE_RULE_TYPES,
    build_flags_snapshot,
    clean_justification,
    is_justifiable,
    verdicts_to_flags,
)
from ai_agents.tools.policy_eval import RuleVerdict


def _violation(rule_type: str, clause: str = "X") -> RuleVerdict:
    return RuleVerdict(
        rule_id=f"rule-{rule_type}",
        rule_type=rule_type,
        applies_to="all",
        source_clause_ref=clause,
        in_policy=False,
        reason=f"{clause} violated. Requires manager pre-approval.",
        requires_approver="manager",
        observed={"amount_usd": 600.0},
    )


def _receipt_rule() -> RuleVerdict:
    return RuleVerdict(
        rule_id="rule-receipt",
        rule_type="receipt_required",
        applies_to="all",
        source_clause_ref="RECEIPT-REQ",
        in_policy=True,
        reason="Receipt required.",
        requires_approver=None,
        observed={"receipt_required": True},
    )


def _flags(verdicts: list[RuleVerdict], justification: str | None, *, has_receipt: bool = True):
    flags, failing = verdicts_to_flags(
        verdicts,
        entry_ref="Grand Hotel",
        has_receipt=has_receipt,
        category="lodging",
        entry_id="e-1",
        justification=justification,
    )
    return flags, failing


@pytest.mark.parametrize("rule_type", sorted(JUSTIFIABLE_RULE_TYPES))
def test_justifiable_violation_becomes_advisory(rule_type: str) -> None:
    (flag,), failing = _flags([_violation(rule_type)], "  Sold-out conference week.  ")
    assert failing == 1  # still a failing verdict (M-A3 guard unchanged)
    assert flag.flag_type == "policy_violation"
    assert flag.severity == "advisory"
    assert flag.justifiable is True
    assert flag.justification == "Sold-out conference week."
    d = flag.to_dict()
    assert d["justification"] == "Sold-out conference week." and d["justifiable"] is True
    assert build_flags_snapshot([flag], 1)["blocking_count"] == 0


@pytest.mark.parametrize("justification", [None, "", "   "])
def test_no_or_blank_justification_stays_blocking(justification: str | None) -> None:
    (flag,), _ = _flags([_violation("amount_cap", "HOTEL-CAP")], justification)
    assert flag.severity == "blocking"
    assert flag.justifiable is True
    assert flag.justification is None


@pytest.mark.parametrize("rule_type", ["category_allow", "cabin_class", "some_future_rule"])
def test_hard_rule_types_ignore_justification(rule_type: str) -> None:
    (flag,), _ = _flags([_violation(rule_type)], "Please allow it.")
    assert flag.severity == "blocking"
    assert flag.justifiable is False
    assert flag.justification is None


def test_missing_receipt_ignores_justification() -> None:
    (flag,), _ = _flags([_receipt_rule()], "I lost it.", has_receipt=False)
    assert flag.flag_type == "missing_receipt"
    assert flag.severity == "blocking"
    assert flag.justifiable is False
    assert flag.justification is None


def test_mixed_entry_keeps_hard_block_and_softens_cap() -> None:
    flags, _ = _flags(
        [_violation("amount_cap", "HOTEL-CAP"), _receipt_rule()], "Conference.", has_receipt=False
    )
    by_type = {f.flag_type: f for f in flags}
    assert by_type["policy_violation"].severity == "advisory"
    assert by_type["missing_receipt"].severity == "blocking"
    assert build_flags_snapshot(flags, 1)["blocking_count"] == 1


def test_approval_time_still_counts_a_justified_violation() -> None:
    (flag,), _ = _flags([_violation("amount_cap", "HOTEL-CAP")], "Conference.")
    assert _is_blocking(flag.to_dict()) is True


def test_clean_justification() -> None:
    assert clean_justification("  a  ") == "a"
    assert clean_justification("   ") is None
    assert clean_justification(None) is None


def test_cabin_class_is_not_justifiable() -> None:
    # AIR-CABIN upgrades need VP pre-approval; there is no VP approval tier.
    assert "cabin_class" not in JUSTIFIABLE_RULE_TYPES
    v = RuleVerdict(
        rule_id="rule-cabin",
        rule_type="cabin_class",
        applies_to="flight",
        source_clause_ref="AIR-CABIN",
        in_policy=False,
        reason="Business class not allowed for a 3h flight. Requires VP approval.",
        requires_approver="VP",
        observed={"cabin_class": "business", "flight_hours": 3},
    )
    (flag,), _ = _flags([v], "Client asked me to fly with them.")
    assert flag.severity == "blocking"
    assert flag.justifiable is False and flag.justification is None
    assert is_justifiable(v) is False


@pytest.mark.parametrize("approver", ["VP", "vp", "CFO", "finance"])
def test_justifiable_type_needing_a_higher_approver_stays_blocking(approver: str) -> None:
    v = dataclasses.replace(_violation("amount_cap", "HOTEL-CAP"), requires_approver=approver)
    (flag,), _ = _flags([v], "Conference.")
    assert flag.severity == "blocking"
    assert flag.justifiable is False and flag.justification is None


def _air_pref(*, supplier: str, verified: bool) -> RuleVerdict:
    observed: dict = {"supplier": supplier, "preferred_suppliers": ["United Airlines"]}
    if not verified:
        observed["supplier_verified"] = False
    return RuleVerdict(
        rule_id="rule-air-pref",
        rule_type="preferred_supplier",
        applies_to="flight",
        source_clause_ref="AIR-PREF",
        in_policy=False,
        reason="Supplier not in the preferred list. Requires manager pre-approval.",
        requires_approver="manager",
        observed=observed,
    )


def test_unverified_supplier_is_not_justifiable() -> None:
    # AIR-PREF with no readable supplier: an evidence failure, like a missing receipt.
    v = _air_pref(supplier="", verified=False)
    (flag,), _ = _flags([v], "It was United, the receipt is blurry.")
    assert flag.severity == "blocking"
    assert flag.justifiable is False and flag.justification is None
    assert is_justifiable(v) is False


def test_verified_non_preferred_supplier_is_justifiable() -> None:
    v = _air_pref(supplier="Spirit Airlines", verified=True)
    (flag,), _ = _flags([v], "Only carrier on that route.")
    assert flag.severity == "advisory"
    assert flag.justifiable is True
    assert flag.justification == "Only carrier on that route."
