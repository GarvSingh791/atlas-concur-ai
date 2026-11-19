"""Grade-entitlement rules: AIR-07, LOD-05, NR-01 (SVPs and above).

Revision ID: 0025_grade_entitlement_rules
Revises: 0024_auth_tokens
Create Date: 2026-11-15

Follow-up (grades).  The accounts track owns 0024_auth_tokens; this migration is chained onto
0024 (integration).

DATA ONLY (no schema change, no enum change — the rules reuse ``category_allow`` /
``all`` with the process ``kind`` ``grade_entitlement`` that
``ai_agents/tools/grade_entitlements.py`` evaluates):

AIR-07, LOD-05 and NR-01 are INSERTED, active, into every policy that carries a
HOTEL-CAP rule (the deterministic-rule policy) — only when that policy has NO rule
citing the clause yet (any status: an archived / admin-edited rule is never re-added,
overwritten or touched).  Every inserted row gets an ``audit_log`` row (actor NULL =
system).  Idempotent; a fresh database is a no-op here when the seed already wrote the
rules.

``downgrade()`` deletes the rows that still equal their literal and that no person
ever touched (no audit row with an actor) — the rows this migration inserted and the
rows a fresh seed wrote — each with an ``audit_log`` row.  An admin-edited or
admin-authored rule is never deleted.

The literals below are FROZEN copies — never import them from the seed module
(``backend/tests/test_fu_grade_entitlements_consistency.py::
test_migration_chain_and_frozen_literals`` asserts they equal the seed).  The data step
and both downgrade guards (literal definition; no actor audit) are proven on a populated
database in ``backend/tests/test_fu_grade_entitlements_db.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0025_grade_entitlement_rules"
down_revision: str | None = "0024_auth_tokens"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# (source_clause_ref, rule_type, applies_to, definition)
NEW_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "AIR-07",
        "category_allow",
        "all",
        {
            "kind": "grade_entitlement",
            "corpus_clause_ref": "AIR-07",
            "entitlement": "airport_extras",
            "min_grade": "SVP",
            "delay_exception_hours": 4,
        },
    ),
    (
        "LOD-05",
        "category_allow",
        "all",
        {
            "kind": "grade_entitlement",
            "corpus_clause_ref": "LOD-05",
            "entitlement": "room_upgrade",
            "min_grade": "SVP",
        },
    ),
    (
        "NR-01",
        "category_allow",
        "all",
        {
            "kind": "grade_entitlement",
            "corpus_clause_ref": "NR-01",
            "entitlement": "lounge_membership",
            "min_grade": "SVP",
        },
    ),
)

_SOURCE = "migration 0025: grade-entitlement rules (AIR-07, LOD-05, NR-01)"

# Insert into every policy that carries a HOTEL-CAP rule and has no rule for :ref yet.
_INSERT_SQL = sa.text(
    """
    WITH targets AS (
        SELECT DISTINCT p.id AS policy_id, p.organization_id
          FROM policies AS p
          JOIN policy_rules AS h
            ON h.policy_id = p.id AND h.source_clause_ref = 'HOTEL-CAP'
         WHERE NOT EXISTS (
               SELECT 1 FROM policy_rules AS x
                WHERE x.policy_id = p.id AND x.source_clause_ref = :ref)
    ), ins AS (
        INSERT INTO policy_rules
            (policy_id, rule_type, applies_to, definition, source_clause_ref, status)
        SELECT t.policy_id, CAST(:rule_type AS policy_rule_type),
               CAST(:applies_to AS policy_applies_to), CAST(:definition AS jsonb), :ref,
               CAST('active' AS policy_rule_status)
          FROM targets AS t
        RETURNING id, policy_id
    )
    INSERT INTO audit_log
        (organization_id, actor_id, action, entity_type, entity_id, before_state, after_state)
    SELECT t.organization_id, NULL, 'policy_rule.seeded', 'policy_rule', ins.id, NULL,
           jsonb_build_object(
               'status', 'active',
               'rule_type', CAST(:rule_type AS text),
               'applies_to', CAST(:applies_to AS text),
               'source_clause_ref', CAST(:ref AS text),
               'definition', CAST(:definition AS jsonb),
               'source', CAST(:source AS text))
      FROM ins JOIN targets AS t ON t.policy_id = ins.policy_id
    RETURNING entity_id
    """
)

_DELETE_SQL = sa.text(
    """
    WITH del AS (
        DELETE FROM policy_rules AS r
         USING policies AS p
         WHERE p.id = r.policy_id
           AND r.source_clause_ref = :ref
           AND r.definition = CAST(:definition AS jsonb)
           AND NOT EXISTS (
               SELECT 1 FROM audit_log AS a
                WHERE a.entity_type = 'policy_rule'
                  AND a.entity_id = r.id
                  AND a.actor_id IS NOT NULL)
     RETURNING r.id, p.organization_id, r.status
    )
    INSERT INTO audit_log
        (organization_id, actor_id, action, entity_type, entity_id, before_state, after_state)
    SELECT organization_id, NULL, 'policy_rule.unseeded', 'policy_rule', id,
           jsonb_build_object('status', CAST(status AS text),
                              'source_clause_ref', CAST(:ref AS text),
                              'definition', CAST(:definition AS jsonb)),
           jsonb_build_object('source', CAST(:source AS text))
      FROM del
    RETURNING entity_id
    """
)


def apply_grade_rules(bind: Any, *, forward: bool) -> dict[str, int]:
    """Insert (forward) or remove (reverse) the grade-entitlement rules on *bind*.

    Returns {"inserted": n, "deleted": n}.  Separate from upgrade() so it is testable
    against a populated database.
    """
    counts = {"inserted": 0, "deleted": 0}
    source = _SOURCE if forward else f"{_SOURCE} (downgrade)"
    for ref, rule_type, applies_to, definition in NEW_RULES:
        params = {"ref": ref, "definition": json.dumps(definition), "source": source}
        if forward:
            rows = bind.execute(
                _INSERT_SQL, {**params, "rule_type": rule_type, "applies_to": applies_to}
            ).fetchall()
            counts["inserted"] += len(rows)
            logger.info("0025: %s — %d rule row(s) inserted.", ref, len(rows))
        else:
            rows = bind.execute(_DELETE_SQL, params).fetchall()
            counts["deleted"] += len(rows)
            logger.info("0025 downgrade: %s — %d seeded rule row(s) removed.", ref, len(rows))
    return counts


def upgrade() -> None:
    apply_grade_rules(op.get_bind(), forward=True)


def downgrade() -> None:
    apply_grade_rules(op.get_bind(), forward=False)
