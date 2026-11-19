"""Integration (policy tracks x team track) — the exception-type catalog covers every
flag type the evaluators can emit (hermetic).

The finance approval filters / CSV export (``backend.app.modules.approval.filters``)
can only find an exception whose ``flag_type`` is in ``EXCEPTION_TYPES``.  The older
scan (``test_pg_filters_unit``) only sees string literals and ``*_FLAG = "..."``
constants, so the policy tracks' flag types — emitted through constants such as
``process_rules.LATE_SUBMISSION`` or ``presubmit.POLICY_ADVISORY`` — slipped past it.

This test parses every producer module in ``ai_agents`` (AST, no execution of the
emitting code paths) and resolves the value of each ``flag_type=<expr>`` keyword and
``"flag_type": <expr>`` dict entry: string literals, module-level constants
(``LATE_SUBMISSION``), attributes of imported modules (``pr.NOT_EVALUATED``) and both
branches of a conditional expression.  Expressions that are not a fixed value (e.g.
``finding.flag_type`` — a ``Finding`` built with a constant elsewhere, which the scan
resolves there) are skipped.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.modules.approval import filters
from database.models.policy_rule import PolicyRuleType

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "ai_agents"


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    return ".".join(rel.parts)


def _flag_exprs(tree: ast.AST) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "flag_type":
                    yield kw.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "flag_type":
                    yield value


def _resolve(expr: ast.expr, module: Any) -> set[str]:
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) and expr.value else set()
    if isinstance(expr, ast.IfExp):
        return _resolve(expr.body, module) | _resolve(expr.orelse, module)
    if isinstance(expr, ast.Name):
        value = getattr(module, expr.id, None)
        return {value} if isinstance(value, str) and value else set()
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        owner = getattr(module, expr.value.id, None)
        if owner is not None and hasattr(owner, "__name__") and hasattr(owner, "__file__"):
            value = getattr(owner, expr.attr, None)
            return {value} if isinstance(value, str) and value else set()
    return set()  # not a fixed value (e.g. finding.flag_type)


def emitted_flag_types() -> dict[str, set[str]]:
    """flag type -> the producer modules that emit it."""
    out: dict[str, set[str]] = {}
    for path in sorted(AGENTS.rglob("*.py")):
        if "tests" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        if "flag_type" not in src:
            continue
        tree = ast.parse(src)
        module = importlib.import_module(_module_name(path))
        for expr in _flag_exprs(tree):
            for value in _resolve(expr, module):
                out.setdefault(value, set()).add(_module_name(path))
    return out


def test_scanner_sees_the_policy_tracks_flag_types() -> None:
    """Guard on the scanner itself: it must resolve constants, not only literals."""
    emitted = emitted_flag_types()
    for expected in (
        "late_submission",  # process_rules.LATE_SUBMISSION (EXP-01)
        "unreconciled_card",  # process_rules.UNRECONCILED_CARD (CARD-03)
        "policy_advisory",  # presubmit.POLICY_ADVISORY (LOD-02 allowance)
        "missing_receipt",  # process_rules.MISSING_RECEIPT (EXP-04)
        "currency_unverified",  # fx_policy.CURRENCY_UNVERIFIED_FLAG
        "possible_split_trip",  # approval agent
    ):
        assert expected in emitted, (expected, sorted(emitted))


def test_catalog_covers_every_emitted_flag_type() -> None:
    emitted = emitted_flag_types()
    missing = {k: sorted(v) for k, v in emitted.items() if k not in filters.EXCEPTION_TYPES}
    assert not missing, f"flag types the finance filters cannot find: {missing}"
    for value in emitted:
        assert filters.EXCEPTION_TYPES[value].kind == filters.KIND_FLAG


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("late_submission", "Late submission"),
        ("unreconciled_card", "Unreconciled card transactions"),
        ("policy_advisory", "Policy advisory"),
        (PolicyRuleType.per_head_cap.value, "Rule: per-head cap"),
        (PolicyRuleType.gift_disclosure.value, "Rule: gift disclosure"),
        (PolicyRuleType.mileage.value, "Rule: mileage"),
    ],
)
def test_new_policy_types_have_readable_labels(value: str, label: str) -> None:
    assert filters.EXCEPTION_TYPES[value].label == label


def test_rows_are_labelled_with_the_new_types() -> None:
    """The CSV / list label path (``exception_types_of``) finds the new flags."""
    doc = {
        "flags": [
            {"flag_type": "late_submission", "rule_type": "category_allow"},
            {"flag_type": "unreconciled_card"},
            {"flag_type": "policy_violation", "rule_type": "per_head_cap"},
        ]
    }
    found = set(filters.exception_types_of(doc))
    assert {"late_submission", "unreconciled_card", "per_head_cap"} <= found
