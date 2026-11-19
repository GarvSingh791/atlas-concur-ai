"""Entry-level policy rules: LOD-02, MEAL-02 / MEAL-04, GRND-01 / 03 / 05 (product gap 2).

Revision ID: 0017_entry_level_policy_rules
Revises: 0016_report_trip_lifecycle
Create Date: 2026-11-14

DATA ONLY — no schema change, no enum change (the new rules reuse the existing
``amount_cap`` / ``category_allow`` rule types with a ``kind`` discriminator that
``ai_agents/tools/entry_rules.py`` evaluates).

1. HOTEL-CAP gains the LOD-02 tolerance keys (<= 15% over the nightly cap is
   auto-approved when booked through the travel tool) and MEALS-PD the MEAL-02
   partial-day keys (75% on departure / return days) and the MEAL-04 exclusion
   clause.  Only a row whose definition still EQUALS the canonical 0015 literal
   (jsonb equality) is rewritten — an admin-changed rule is left alone and reported.
2. GRND-01 (rail class), GRND-03 (airport parking 7 days, economy lot) and GRND-05
   (tips <= 20%) are INSERTED, active, into every policy that carries a HOTEL-CAP
   rule (the deterministic-rule policy) — only when that policy has NO rule citing
   the clause yet (any status: an archived / edited one is never re-added or touched).

Safety: rule ids of existing rows are stable (updated in place); every rewritten or
inserted row gets an ``audit_log`` row (actor NULL = system); idempotent; a fresh
database is a no-op (the seed, run afterwards, writes the new values).
``downgrade()`` deletes only inserted rows that still equal their literal, restores
the two definitions that still equal the new literal, each with its own audit row.

The literals below are FROZEN copies — never import them from the seed module.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0017_entry_level_policy_rules"
down_revision: str | None = "0016_report_trip_lifecycle"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

_TIER2 = [
    "Chicago",
    "Seattle",
    "Washington DC",
    "Toronto",
    "Sydney",
    "Dubai",
    "Frankfurt",
    "Amsterdam",
    "Dublin",
]
_TIER1 = [
    "New York",
    "San Francisco",
    "London",
    "Tokyo",
    "Singapore",
    "Hong Kong",
    "Zurich",
    "Geneva",
    "Boston",
    "Los Angeles",
    "Paris",
]
_ALIASES = {"New York": ["New York City", "NYC", "Manhattan"]}

# == migration 0015 NEW_HOTEL_CAP
OLD_HOTEL_CAP: dict[str, Any] = {
    "currency": "USD",
    "per": "night",
    "basis": "room_plus_tax",
    "corpus_clause_ref": "LOD-01",
    "tiers": [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "cap": 160,
        },
        {"tier": "tier2", "label": "Tier 2", "cities": _TIER2, "cap": 225},
        {"tier": "tier1", "label": "Tier 1", "cities": _TIER1, "aliases": _ALIASES, "cap": 300},
    ],
    "exception_approver": "manager",
}
NEW_HOTEL_CAP: dict[str, Any] = {
    **OLD_HOTEL_CAP,
    "auto_approve_over_cap_pct": 15,
    "auto_approve_clause_ref": "LOD-02",
    "auto_approve_requires": "booked_through_tool",
}

# == migration 0015 NEW_MEALS_PD
OLD_MEALS_PD: dict[str, Any] = {
    "currency": "USD",
    "per": "day",
    "corpus_clause_ref": "MEAL-01",
    "rates": [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "amount": 65,
        },
        {"tier": "tier2", "label": "Tier 2", "cities": _TIER2, "amount": 75},
        {"tier": "tier1", "label": "Tier 1", "cities": _TIER1, "aliases": _ALIASES, "amount": 85},
    ],
    "exclusions": ["alcohol"],
    "group_meal_threshold": 3,
    "group_meal_approver": "manager",
}
NEW_MEALS_PD: dict[str, Any] = {
    **OLD_MEALS_PD,
    "partial_day_pct": 75,
    "partial_day_clause_ref": "MEAL-02",
    "exclusions_clause_ref": "MEAL-04",
}

RULE_CHANGES: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("HOTEL-CAP", OLD_HOTEL_CAP, NEW_HOTEL_CAP),
    ("MEALS-PD", OLD_MEALS_PD, NEW_MEALS_PD),
)

# (source_clause_ref, rule_type, applies_to, definition)
NEW_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "GRND-01",
        "category_allow",
        "all",
        {
            "kind": "rail_class",
            "corpus_clause_ref": "GRND-01",
            "expense_categories": ["ground_transport"],
            "ground_subtypes": ["rail"],
            "standard_classes": ["standard", "economy"],
            "premium_classes": ["first", "business"],
            "premium_min_hours": 3,
            "premium_condition": "cheaper than or comparable to flying",
            "exception_approver": "manager",
        },
    ),
    (
        "GRND-03",
        "amount_cap",
        "all",
        {
            "kind": "parking_days",
            "corpus_clause_ref": "GRND-03",
            "expense_categories": ["ground_transport"],
            "ground_subtypes": ["parking"],
            "max_days": 7,
            "lot": "economy",
            "exception_approver": "manager",
        },
    ),
    (
        "GRND-05",
        "amount_cap",
        "all",
        {
            "kind": "tip_pct",
            "corpus_clause_ref": "GRND-05",
            "expense_categories": ["ground_transport"],
            "ground_subtypes": ["rideshare_taxi"],
            "max_tip_pct": 20,
            "exception_approver": "manager",
        },
    ),
)

_SOURCE = "migration 0017: entry-level policy rules (LOD-02, MEAL-02/04, GRND-01/03/05)"

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
           AND r.created_by IS NULL
           AND r.definition = CAST(:definition AS jsonb)
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


def apply_entry_level_rules(bind: Any, *, forward: bool) -> dict[str, int]:
    """Apply (forward) or reverse this revision on *bind* (a SQLAlchemy connection).

    Returns counts {"rewritten": n, "inserted": n, "deleted": n}.  Separate from
    upgrade() so it is testable against a populated database.
    """
    counts = {"rewritten": 0, "inserted": 0, "deleted": 0}
    action = "policy_rule.definition_updated" if forward else "policy_rule.definition_reverted"
    source = _SOURCE if forward else f"{_SOURCE} (downgrade)"
    if not forward:
        for ref, rule_type, applies_to, definition in NEW_RULES:
            deleted = bind.execute(
                _DELETE_SQL,
                {"ref": ref, "definition": json.dumps(definition), "source": source},
            ).fetchall()
            counts["deleted"] += len(deleted)
            logger.info("0017 downgrade: %s — %d seeded rule row(s) removed.", ref, len(deleted))
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
        logger.info("0017: %s — %d rule row(s) rewritten.", ref, len(updated))
        if forward:
            unmatched = bind.execute(
                _UNMATCHED_SQL, {"ref": ref, "old": json.dumps(old), "new": json.dumps(new)}
            ).scalar()
            if unmatched:
                logger.warning(
                    "0017: %d active %s rule(s) match neither the canonical nor the new "
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
            logger.info("0017: %s — %d rule row(s) inserted.", ref, len(inserted))
    return counts


def upgrade() -> None:
    apply_entry_level_rules(op.get_bind(), forward=True)


def downgrade() -> None:
    apply_entry_level_rules(op.get_bind(), forward=False)
