"""Hermetic static guard for migration 0015 (canonical HOTEL-CAP / MEALS-PD figures).

Does NOT touch a database — imports the migration module by path and asserts:
  * the revision chains onto 0014_approval_step_snapshot;
  * the FROZEN ``OLD_*`` literals are exactly the stub definitions the eval-db
    fixture (evals/datasets/policy_rules.json) and existing databases carry;
  * the FROZEN ``NEW_*`` literals are exactly what the seed now derives from the
    canonical corpus figures (policy_content.RULES), lowest tier first;
  * the document-section literals match the seed's DOCUMENT_TEXT.
The live apply / idempotency / downgrade is covered by test_issue6_canonical_db.py
and the full chain by test_migration_chain.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from database.seeds.corpus import figures as F
from database.seeds.policy_content import DOCUMENT_TEXT, RULES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT / "database" / "migrations" / "versions" / "0015_canonical_policy_figures.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0015", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Keys migration 0017 (product gap 2: LOD-02 / MEAL-02 / MEAL-04) added on top of the
# 0015 canonical definitions — the seed carries both, so 0015's literal is the seed
# definition without them.
_KEYS_0017 = frozenset(
    {
        "auto_approve_over_cap_pct",
        "auto_approve_clause_ref",
        "auto_approve_requires",
        "partial_day_pct",
        "partial_day_clause_ref",
        "exclusions_clause_ref",
    }
)


def _seed_definition(ref: str) -> dict:
    definition = next(r["definition"] for r in RULES if r["clause_ref"] == ref)
    return {k: v for k, v in definition.items() if k not in _KEYS_0017}


def test_revision_chain_links_onto_0014() -> None:
    module = _load_module()
    assert module.revision == "0015_canonical_policy_figures"
    assert module.down_revision == "0014_approval_step_snapshot"
    assert callable(module.upgrade) and callable(module.downgrade)


def test_old_literals_are_the_stub_definitions() -> None:
    module = _load_module()
    legacy = {
        r["source_clause_ref"]: r["definition"]
        for r in json.loads(
            (_REPO_ROOT / "evals" / "datasets" / "policy_rules.json").read_text(encoding="utf-8")
        )
    }
    assert module.OLD_HOTEL_CAP == legacy["HOTEL-CAP"]
    assert module.OLD_MEALS_PD == legacy["MEALS-PD"]


def test_new_literals_are_the_canonical_seed_definitions() -> None:
    module = _load_module()
    assert module.NEW_HOTEL_CAP == _seed_definition("HOTEL-CAP")
    assert module.NEW_MEALS_PD == _seed_definition("MEALS-PD")
    assert module.NEW_HOTEL_CAP["tiers"][0] == {
        "tier": "tier3",
        "label": "Tier 3",
        "description": "All other locations",
        "default": True,
        "cap": F.LODGING_CAP["tier3"],
    }
    assert module.NEW_MEALS_PD["rates"][0]["amount"] == F.PER_DIEM["tier3"]


def test_document_sections() -> None:
    module = _load_module()
    assert module.NEW_HOTEL_SECTION in DOCUMENT_TEXT
    assert module.NEW_MEALS_SECTION in DOCUMENT_TEXT
    assert module.OLD_HOTEL_SECTION not in DOCUMENT_TEXT
    assert "USD 250" in module.OLD_HOTEL_SECTION and "USD 180" in module.OLD_HOTEL_SECTION
    assert "USD 75 per day" in module.OLD_MEALS_SECTION
    assert module.OLD_HOTEL_SECTION.startswith("[HOTEL-CAP]")
    assert module.OLD_MEALS_SECTION.startswith("[MEALS-PD]")


def test_rule_changes_cover_both_rules() -> None:
    module = _load_module()
    assert [ref for ref, _old, _new in module.RULE_CHANGES] == ["HOTEL-CAP", "MEALS-PD"]
