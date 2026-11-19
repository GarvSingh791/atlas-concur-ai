"""QA round — process-rule flags no longer pose as a storage rule type (hermetic).

The browser QA run found CARD-03 "unreconciled card" flags stored with
``rule_type=category_allow`` (the column type the process rule row is stored under,
``process_rules.PROCESS_KIND_SHAPES``), so the finance "Rule: category not allowed"
filter, its count and the CSV "Exception types" column counted reports with no
category problem at all.

Now:
  * a process-rule flag carries its own rule type — the process ``kind``
    (``card_reconciliation``, ``submission_deadline``, ...), with its own label;
  * flags already stored with the old shape are mapped at READ time
    (``effective_rule_type``): ``unreconciled_card`` -> ``card_reconciliation``,
    ``late_submission`` -> ``submission_deadline``; any other process-rule flag
    stored under ``category_allow`` / ``amount_cap`` no longer claims that type.
"""

from __future__ import annotations

from ai_agents.agents import process_checks
from ai_agents.tools import process_rules as pr
from backend.app.modules.approval import filters
from database.seeds.policy_content import PROCESS_RULES


def _ref(clause: str) -> process_checks.RuleRef:
    rule = next(r for r in PROCESS_RULES if r["clause_ref"] == clause)
    return process_checks.RuleRef("rule-1", rule["rule_type"], rule["definition"])


def _finding(flag_type: str, clause: str) -> pr.Finding:
    return pr.Finding(clause_ref=clause, flag_type=flag_type, severity="advisory", reason="r")


def test_card03_flag_carries_its_own_rule_type() -> None:
    flag = process_checks.flag_dict(
        _finding(pr.UNRECONCILED_CARD, "CARD-03"), rule=_ref("CARD-03"), entry_ref="card"
    )
    assert flag["rule_type"] == "card_reconciliation"
    assert filters.exception_types_of({"flags": [flag]}) == [
        "unreconciled_card",
        "card_reconciliation",
    ]


def test_every_process_flag_uses_its_kind_not_the_storage_type() -> None:
    for rule in PROCESS_RULES:
        flag = process_checks.flag_dict(
            _finding("approval_required", rule["clause_ref"]),
            rule=_ref(rule["clause_ref"]),
            entry_ref="x",
        )
        assert flag["rule_type"] == rule["definition"]["kind"]
        assert flag["rule_type"] not in ("category_allow", "amount_cap")
        assert "category_allow" not in filters.exception_types_of({"flags": [flag]})
        assert "amount_cap" not in filters.exception_types_of({"flags": [flag]})


def test_process_rule_types_have_readable_labels() -> None:
    expected = {
        "card_reconciliation": "Rule: card reconciliation",
        "submission_deadline": "Rule: submission deadline",
        "advance_booking": "Rule: advance booking",
        "trip_change_reapproval": "Rule: trip cost change",
        "lowest_fare": "Rule: lowest fare",
    }
    for value, label in expected.items():
        etype = filters.EXCEPTION_TYPES[value]
        assert etype.label == label and etype.kind == filters.KIND_RULE


def test_legacy_card03_flag_is_mapped_at_read_time() -> None:
    legacy = {
        "flag_type": "unreconciled_card",
        "rule_type": "category_allow",
        "source_clause_ref": "CARD-03",
        "observed": {"process_rule": True},
    }
    assert filters.effective_rule_type(legacy) == "card_reconciliation"
    found = filters.exception_types_of({"flags": [legacy]})
    assert "category_allow" not in found
    assert found == ["unreconciled_card", "card_reconciliation"]
    assert filters.exception_labels(found) == [
        "Unreconciled card transactions",
        "Rule: card reconciliation",
    ]


def test_legacy_late_submission_and_other_process_flags() -> None:
    late = {"flag_type": "late_submission", "rule_type": "category_allow"}
    assert filters.effective_rule_type(late) == "submission_deadline"
    pta02 = {
        "flag_type": "approval_required",
        "rule_type": "category_allow",
        "observed": {"process_rule": True},
    }
    assert filters.effective_rule_type(pta02) is None
    assert filters.exception_types_of({"flags": [pta02]}) == ["approval_required"]


def test_genuine_rule_flags_keep_their_rule_type() -> None:
    ground = {"flag_type": "policy_violation", "rule_type": "category_allow"}
    assert filters.effective_rule_type(ground) == "category_allow"
    assert "category_allow" in filters.exception_types_of({"flags": [ground]})
    # AIR-04 role exception: a process-rule modifier of the cabin rule stays cabin_class.
    air04 = {
        "flag_type": "approval_required",
        "rule_type": "cabin_class",
        "observed": {"process_rule": True},
    }
    assert filters.effective_rule_type(air04) == "cabin_class"
