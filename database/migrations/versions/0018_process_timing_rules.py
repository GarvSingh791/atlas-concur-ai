"""Process & timing policy rules: EXP-01, EXP-04, PTA-02, PTA-05, AIR-04, AIR-06, CARD-03.

Revision ID: 0018_process_timing_rules
Revises: 0017_entry_level_policy_rules
Create Date: 2026-11-14

Product gap 2, stage 2.

SCHEMA (additive, safe on a populated table): one NULLABLE column
``expenses.receipt_affidavit_at`` (timestamptz) — when the owner signed the EXP-04
missing-receipt affidavit.  A nullable add without a default is a catalog-only change
in Postgres (no table rewrite).

DATA (no enum change — the new rules reuse existing rule types with a ``kind``
discriminator that ``ai_agents/tools/process_rules.py`` evaluates):

1. RECEIPT-REQ gains the EXP-04 affidavit keys ($75, 3 per quarter) and AIR-CABIN the
   AIR-04 role-exception keys (SVP business class on flights > 6 h).  Only a row whose
   definition still EQUALS the canonical seed literal (jsonb equality) is rewritten —
   an admin-changed rule is left alone and reported at WARNING.
2. EXP-01, PTA-02, PTA-05, AIR-06 and CARD-03 are INSERTED, active, into every policy
   that carries a HOTEL-CAP rule (the deterministic-rule policy) — only when that
   policy has NO rule citing the clause yet (any status: an archived / edited one is
   never re-added or touched).

Every rewritten or inserted row gets an ``audit_log`` row (actor NULL = system);
idempotent; a fresh database only gets the column (the seed writes the rules).
``downgrade()`` deletes the process rows that still equal their literal and that no
person ever touched (no audit row with an actor) — the rows this migration inserted
AND the rows a fresh seed wrote (integration fix: those carry ``created_by``); an
admin-authored or admin-edited rule is never deleted.  It restores the two definitions
that still equal the new literal (each audited) and drops the column.

The literals below are FROZEN copies — never import them from the seed module.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0018_process_timing_rules"
down_revision: str | None = "0017_entry_level_policy_rules"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

# == database/seeds/policy_content.py RULES[RECEIPT-REQ] before this revision
OLD_RECEIPT_REQ: dict[str, Any] = {
    "threshold_usd": 25,
    "always_required_categories": ["airfare", "lodging"],
    "consequence": "flagged_for_review",
}
NEW_RECEIPT_REQ: dict[str, Any] = {
    **OLD_RECEIPT_REQ,
    "affidavit_max_usd": 75,
    "affidavit_max_per_quarter": 3,
    "affidavit_clause_ref": "EXP-04",
}

# == database/seeds/policy_content.py RULES[AIR-CABIN] before this revision
OLD_AIR_CABIN: dict[str, Any] = {
    "tiers": [
        {"max_hours": 6, "allowed_classes": ["economy"]},
        {"min_hours": 6, "max_hours": 10, "allowed_classes": ["economy", "premium_economy"]},
        {"min_hours": 10, "allowed_classes": ["economy", "premium_economy", "business"]},
    ],
    "override_approver": "VP",
}
NEW_AIR_CABIN: dict[str, Any] = {
    **OLD_AIR_CABIN,
    "role_exception_class": "business",
    "role_exception_min_hours": 6,
    "role_exception_role": "SVP",
    "role_exception_clause_ref": "AIR-04",
}

RULE_CHANGES: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("RECEIPT-REQ", OLD_RECEIPT_REQ, NEW_RECEIPT_REQ),
    ("AIR-CABIN", OLD_AIR_CABIN, NEW_AIR_CABIN),
)

# (source_clause_ref, rule_type, applies_to, definition)
NEW_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "EXP-01",
        "category_allow",
        "all",
        {
            "kind": "submission_deadline",
            "corpus_clause_ref": "EXP-01",
            "deadline_days": 30,
            "stale_days": 60,
            "stale_approver": "VP",
        },
    ),
    (
        "PTA-02",
        "category_allow",
        "flight",
        {
            "kind": "advance_booking",
            "corpus_clause_ref": "PTA-02",
            "domestic_days": 14,
            "international_days": 21,
            "exception_approver": "manager",
        },
    ),
    (
        "PTA-05",
        "amount_cap",
        "all",
        {
            "kind": "trip_change_reapproval",
            "corpus_clause_ref": "PTA-05",
            "max_increase_pct": 10,
            "exception_approver": "manager",
        },
    ),
    (
        "AIR-06",
        "amount_cap",
        "flight",
        {
            "kind": "lowest_fare",
            "corpus_clause_ref": "AIR-06",
            "domestic_tolerance_usd": 150,
            "international_tolerance_usd": 400,
            "exception_approver": "manager",
        },
    ),
    (
        "CARD-03",
        "category_allow",
        "all",
        {
            "kind": "card_reconciliation",
            "corpus_clause_ref": "CARD-03",
            "deadline_days": 30,
        },
    ),
)

_SOURCE = "migration 0018: process & timing rules (EXP-01/04, PTA-02/05, AIR-04/06, CARD-03)"

_UPDATE_SQL = sa.text(
    """
    WITH upd AS (
        UPDATE policy_rules AS r
           SET definition = CAST(:new AS jsonb), updated_at = now()
          FROM policies AS p
         WHERE p.id = r.policy_id
           AND r.source_clause_ref = :ref
           AND r.definition = CAST(:old AS jsonb)
     RETURNING r.id, p.organization_id
    )
    INSERT INTO audit_log
        (organization_id, actor_id, action, entity_type, entity_id, before_state, after_state)
    SELECT organization_id, NULL, :action, 'policy_rule', id,
           jsonb_build_object('definition', CAST(:old AS jsonb)),
           jsonb_build_object('definition', CAST(:new AS jsonb), 'source', CAST(:source AS text))
      FROM upd
    RETURNING entity_id
    """
)

_UNMATCHED_SQL = sa.text(
    """
    SELECT count(*)
      FROM policy_rules
     WHERE source_clause_ref = :ref
       AND status = 'active'
       AND definition <> CAST(:old AS jsonb)
       AND definition <> CAST(:new AS jsonb)
    """
)

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
           -- Integration: a SEEDED database's rows carry created_by (the seed's
           -- finance admin) and no audit history; they are removed too.  A rule any
           -- person created, edited or (de)activated through the policy page has an
           -- audit row with an actor, and is never deleted.
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


def apply_process_rules(bind: Any, *, forward: bool) -> dict[str, int]:
    """Apply (forward) or reverse the DATA part of this revision on *bind*.

    Returns counts {"rewritten": n, "inserted": n, "deleted": n}.  Separate from
    upgrade() so it is testable against a populated database.
    """
    counts = {"rewritten": 0, "inserted": 0, "deleted": 0}
    action = "policy_rule.definition_updated" if forward else "policy_rule.definition_reverted"
    source = _SOURCE if forward else f"{_SOURCE} (downgrade)"
    if not forward:
        for ref, _rule_type, _applies_to, definition in NEW_RULES:
            deleted = bind.execute(
                _DELETE_SQL,
                {"ref": ref, "definition": json.dumps(definition), "source": source},
            ).fetchall()
            counts["deleted"] += len(deleted)
            logger.info("0018 downgrade: %s — %d seeded rule row(s) removed.", ref, len(deleted))
    for ref, old, new in RULE_CHANGES:
        before, after = (old, new) if forward else (new, old)
        updated = bind.execute(
            _UPDATE_SQL,
            {
                "ref": ref,
                "old": json.dumps(before),
                "new": json.dumps(after),
                "action": action,
                "source": source,
            },
        ).fetchall()
        counts["rewritten"] += len(updated)
        logger.info("0018: %s — %d rule row(s) rewritten.", ref, len(updated))
        if forward:
            unmatched = bind.execute(
                _UNMATCHED_SQL, {"ref": ref, "old": json.dumps(old), "new": json.dumps(new)}
            ).scalar()
            if unmatched:
                logger.warning(
                    "0018: %d active %s rule(s) match neither the canonical nor the new "
                    "definition and were left unchanged — review them in /policy.",
                    unmatched,
                    ref,
                )
    if forward:
        for ref, rule_type, applies_to, definition in NEW_RULES:
            inserted = bind.execute(
                _INSERT_SQL,
                {
                    "ref": ref,
                    "rule_type": rule_type,
                    "applies_to": applies_to,
                    "definition": json.dumps(definition),
                    "source": source,
                },
            ).fetchall()
            counts["inserted"] += len(inserted)
            logger.info("0018: %s — %d rule row(s) inserted.", ref, len(inserted))
    return counts


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column("receipt_affidavit_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    apply_process_rules(op.get_bind(), forward=True)


def downgrade() -> None:
    apply_process_rules(op.get_bind(), forward=False)
    op.drop_column("expenses", "receipt_affidavit_at")
