"""Grade-entitlement rules (follow-up, grades): AIR-07, LOD-05, NR-01.

Each TEP-FY26 clause that grants something only to "SVPs and above" (beyond AIR-04,
which modifies the AIR-CABIN rule) becomes a PROCESS ``kind`` rule
(``grade_entitlement``): never evaluated per line by PolicyEvalTool, but by the
pre-submit / approval wiring (``ai_agents/agents/grade_checks.py``) from the expense
OWNER's grade of record.  The threshold is the ladder's
``SENIOR_EXCEPTION_MIN_GRADE`` and AIR-07's delay figure comes from the corpus
figures, so the rules, the corpus text (DEF-10, FAQ) and the ladder cannot drift
(``backend/tests/test_fu_grade_entitlements_consistency.py``).

Seeded for fresh databases; migration ``0025_grade_entitlement_rules`` adds them to
live ones (frozen copies there).
"""

from __future__ import annotations

from config.constants.grades import SENIOR_EXCEPTION_MIN_GRADE
from database.seeds.corpus import figures as F

AIRPORT_EXTRAS_DEFINITION: dict = {
    "kind": "grade_entitlement",
    "corpus_clause_ref": "AIR-07",
    "entitlement": "airport_extras",
    "min_grade": SENIOR_EXCEPTION_MIN_GRADE,
    "delay_exception_hours": F.LOUNGE_DELAY_EXCEPTION_HOURS,
}

ROOM_UPGRADE_DEFINITION: dict = {
    "kind": "grade_entitlement",
    "corpus_clause_ref": "LOD-05",
    "entitlement": "room_upgrade",
    "min_grade": SENIOR_EXCEPTION_MIN_GRADE,
}

LOUNGE_MEMBERSHIP_DEFINITION: dict = {
    "kind": "grade_entitlement",
    "corpus_clause_ref": "NR-01",
    "entitlement": "lounge_membership",
    "min_grade": SENIOR_EXCEPTION_MIN_GRADE,
}

GRADE_RULES: list[dict] = [
    {
        "clause_ref": "AIR-07",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": AIRPORT_EXTRAS_DEFINITION,
    },
    {
        "clause_ref": "LOD-05",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": ROOM_UPGRADE_DEFINITION,
    },
    {
        "clause_ref": "NR-01",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": LOUNGE_MEMBERSHIP_DEFINITION,
    },
]
