"""Hermetic static guard for migration 0019 (product gap 2: spend caps, gifts, mileage).

Does NOT touch a database — imports the migration by path and asserts:
  * it chains onto 0018_process_timing_rules (re-chained by the integration);
  * its FROZEN rule literals are exactly what the seed derives from the canonical
    figures today (``spend_rules_content.SPEND_RULES``), so a fresh database and a
    migrated one enforce the same rules;
  * the enum values it adds are the model's new values.
The live apply / idempotency / downgrade is covered by test_pg2_spend_mileage_db.py
and the full chain by test_migration_chain.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from database.models.expense import ExpenseCategory
from database.models.policy_rule import PolicyRuleType
from database.seeds.spend_rules_content import SPEND_RULES

_PATH = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "0019_spend_caps_mileage.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0019", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain() -> None:
    module = _load()
    assert module.revision == "0019_spend_caps_mileage"
    assert module.down_revision == "0018_process_timing_rules"
    assert callable(module.upgrade) and callable(module.downgrade)


def test_frozen_rules_equal_the_seed() -> None:
    module = _load()
    frozen = [
        {"clause_ref": ref, "rule_type": rt, "applies_to": at, "definition": d}
        for ref, rt, at, d in module.NEW_RULES
    ]
    assert frozen == SPEND_RULES


def test_enum_values_match_the_models() -> None:
    module = _load()
    assert set(module.NEW_EXPENSE_CATEGORIES) == {"mileage", "entertainment", "gift"}
    assert set(module.NEW_EXPENSE_CATEGORIES) <= {c.value for c in ExpenseCategory}
    assert set(module.NEW_RULE_TYPES) <= {t.value for t in PolicyRuleType}
