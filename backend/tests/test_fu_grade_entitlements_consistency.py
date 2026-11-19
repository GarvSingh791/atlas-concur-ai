"""Follow-up (grades): rules == ladder == corpus clauses == migration (hermetic).

The AIR-07 / LOD-05 / NR-01 rules, the grade ladder's clause-stated entitlements, the
policy corpus (AIR-07, LOD-05, NR-01, DEF-10, FAQ "Traveler Grades") and the frozen
literals of migration 0025 must agree, so the RAG answers and the rule engine name the
same thresholds.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from ai_agents.tools import grade_entitlements as ge
from config.constants import grades as G
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus import figures as F
from database.seeds.grade_rules_content import GRADE_RULES
from database.seeds.policy_content import PROCESS_RULES

_PATH = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "0025_grade_entitlement_rules.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0025", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unit(unit_id: str) -> str:
    for doc in ALL_DOCUMENTS:
        for unit in doc.units:
            if unit.id == unit_id:
                return unit.text
    raise AssertionError(f"corpus unit {unit_id} not found")


def test_migration_chain_and_frozen_literals() -> None:
    module = _load()
    assert module.revision == "0025_grade_entitlement_rules"
    # The accounts track owns 0024; the integrator re-chains this onto it.
    assert module.down_revision in ("0023_entry_release_lineage", "0024_auth_tokens")
    frozen = [
        {"clause_ref": ref, "rule_type": rt, "applies_to": at, "definition": d}
        for ref, rt, at, d in module.NEW_RULES
    ]
    assert frozen == GRADE_RULES


def test_seeded_with_the_process_rules() -> None:
    for rule in GRADE_RULES:
        assert rule in PROCESS_RULES


def test_rules_match_the_ladder_entitlements() -> None:
    ladder = {e.clause_id: e for e in G.ENTITLEMENTS if e.clause_id != "AIR-04"}
    rules = {r["clause_ref"]: r["definition"] for r in GRADE_RULES}
    assert set(rules) == set(ladder)
    for clause, defn in rules.items():
        assert defn["min_grade"] == ladder[clause].min_grade == G.SENIOR_EXCEPTION_MIN_GRADE
        assert ladder[clause].rule_enforced is True
        assert ge.DEFAULT_CLAUSE[defn["entitlement"]] == clause == defn["corpus_clause_ref"]


def test_rules_match_the_clause_text() -> None:
    short = G.grade(G.SENIOR_EXCEPTION_MIN_GRADE)
    assert short is not None
    svp_plus = f"{short.short}s+"
    air07, lod05, nr01 = _unit("AIR-07"), _unit("LOD-05"), _unit("NR-01")
    delay = next(r for r in GRADE_RULES if r["clause_ref"] == "AIR-07")["definition"][
        "delay_exception_hours"
    ]
    assert delay == F.LOUNGE_DELAY_EXCEPTION_HOURS
    assert (
        f"Early-boarding and lounge fees are not reimbursable (except for {svp_plus} or "
        f"delays > {delay} hours)"
    ) in air07
    assert f"suites/upgrades not reimbursable except {svp_plus}" in lod05
    assert f"lounge memberships (except {svp_plus})" in nr01
    faq = _unit("FAQ-Q58")
    assert f"more than {delay} hours (see AIR-07)" in faq and "(see NR-01)" in faq
    def10 = _unit("DEF-10")
    for clause in ("AIR-07", "LOD-05", "NR-01"):
        assert f"({clause})" in def10
