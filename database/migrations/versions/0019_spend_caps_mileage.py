"""Spend caps, gift disclosure and mileage (product gap 2): MEAL-05, ENT-02, ENT-04, MIL-01..03.

Revision ID: 0019_spend_caps_mileage
Revises: 0018_process_timing_rules
Create Date: 2026-11-14

Schema (additive, safe on a populated database — no table rewrite):
  * enum values: ``expense_category`` += mileage / entertainment / gift;
    ``policy_rule_type`` += per_head_cap / gift_disclosure / mileage
    (``ADD VALUE IF NOT EXISTS`` in an autocommit block — PostgreSQL forbids using
    a value added in the same transaction);
  * ``expenses`` += nullable ``attendee_count`` INTEGER (MEAL-05 / ENT-02 per-head
    caps), ``disclosure`` JSONB (ENT-04) and ``mileage`` JSONB (MIL-01..03), and
    ``ck_expenses_attendee_count_positive`` (added NOT VALID, then validated — only
    a SHARE UPDATE EXCLUSIVE lock while every existing row is NULL).

Data (the migration-0015 pattern):
  * for every organisation's stub policy (``document_type IS NULL`` and the stub
    name) the six structured rules below are INSERTED, active, only where that
    policy has no rule citing the same clause yet — an existing rule (e.g. one a
    finance user authored, edited or archived) is never touched or duplicated;
  * every inserted row gets an ``audit_log`` row (actor NULL = system) with the
    full definition;
  * idempotent; a fresh database is a no-op (the seed runs afterwards and writes
    the same rules from ``database/seeds/spend_rules_content.py``).

Downgrade (symmetric, fail-closed):
  * REFUSES while any expense uses a new category — pre-0019 code cannot load
    such a row, and deleting a claimant's expenses is never a migration's call;
  * deletes every policy rule of a new rule type (pre-0019 code cannot load
    them), each with an audit row holding its full definition — an admin-edited
    one is reported at WARNING;
  * drops the constraint and the three columns.
  PostgreSQL cannot drop an enum value, so the six values stay (unused; the
  upgrade re-adds them idempotently).

The rule literals below are FROZEN copies — never import them from the seed
module, which may change after this revision.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0019_spend_caps_mileage"
down_revision: str | None = "0018_process_timing_rules"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

STUB_POLICY_NAME = "Atlas Global Travel & Expense Policy"

NEW_EXPENSE_CATEGORIES: tuple[str, ...] = ("mileage", "entertainment", "gift")
NEW_RULE_TYPES: tuple[str, ...] = ("per_head_cap", "gift_disclosure", "mileage")

_TIER2_CITIES: list[str] = [
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
_TIER1_CITIES: list[str] = [
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
_ALIASES: dict[str, list[str]] = {"New York": ["New York City", "NYC", "Manhattan"]}


def _tiers(tier3: int, tier2: int, tier1: int) -> list[dict[str, Any]]:
    return [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "cap": tier3,
        },
        {"tier": "tier2", "label": "Tier 2", "cities": list(_TIER2_CITIES), "cap": tier2},
        {
            "tier": "tier1",
            "label": "Tier 1",
            "cities": list(_TIER1_CITIES),
            "aliases": {k: list(v) for k, v in _ALIASES.items()},
            "cap": tier1,
        },
    ]


# (source_clause_ref, rule_type, applies_to, definition) — FROZEN.
NEW_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "MEAL-05",
        "per_head_cap",
        "meals",
        {
            "currency": "USD",
            "per": "head",
            "corpus_clause_ref": "MEAL-05",
            "min_attendees": 2,
            "tiers": _tiers(60, 75, 90),
            "exception_approver": "manager",
        },
    ),
    (
        "ENT-02",
        "per_head_cap",
        "all",
        {
            "currency": "USD",
            "per": "head",
            "corpus_clause_ref": "ENT-02",
            "expense_categories": ["entertainment"],
            "tiers": _tiers(100, 120, 150),
            "exception_approver": "manager",
        },
    ),
    (
        "ENT-04",
        "gift_disclosure",
        "all",
        {
            "currency": "USD",
            "corpus_clause_ref": "ENT-04",
            "threshold_usd": 100,
            "expense_categories": ["gift", "entertainment"],
            "disclosure_categories": ["gift"],
            "government_official_approver": "Legal/Compliance",
        },
    ),
    (
        "MIL-01",
        "mileage",
        "all",
        {
            "check": "rate",
            "currency": "USD",
            "rate_per_mile": "0.70",
            "rate_basis": "IRS standard business mileage rate",
            "expense_categories": ["mileage"],
            "corpus_clause_ref": "MIL-01",
        },
    ),
    (
        "MIL-02",
        "mileage",
        "all",
        {
            "check": "commute_offset",
            "expense_categories": ["mileage"],
            "corpus_clause_ref": "MIL-02",
        },
    ),
    (
        "MIL-03",
        "mileage",
        "all",
        {
            "check": "air_cap",
            "max_one_way_miles": 300,
            "exception_approver": "manager",
            "expense_categories": ["mileage"],
            "corpus_clause_ref": "MIL-03",
        },
    ),
)

_SOURCE = "migration 0019: TEP-FY26 spend caps / gift disclosure / mileage rules"

_INSERT_SQL = sa.text(
    """
    WITH ins AS (
        INSERT INTO policy_rules
            (id, policy_id, rule_type, applies_to, definition, source_clause_ref, status,
             created_at, updated_at)
        SELECT gen_random_uuid(), p.id, CAST(:rule_type AS policy_rule_type),
               CAST(:applies_to AS policy_applies_to), CAST(:definition AS jsonb), :ref,
               'active', now(), now()
          FROM policies AS p
         WHERE p.document_type IS NULL
           AND p.name = :name
           AND NOT EXISTS (
               SELECT 1 FROM policy_rules AS r
                WHERE r.policy_id = p.id AND r.source_clause_ref = :ref
           )
        RETURNING id, policy_id
    )
    INSERT INTO audit_log
        (organization_id, actor_id, action, entity_type, entity_id, before_state, after_state)
    SELECT p.organization_id, NULL, 'policy_rule.created', 'policy_rule', ins.id, NULL,
           jsonb_build_object(
               'rule_type', CAST(:rule_type AS text),
               'applies_to', CAST(:applies_to AS text),
               'source_clause_ref', CAST(:ref AS text),
               'status', 'active',
               'definition', CAST(:definition AS jsonb),
               'source', CAST(:source AS text))
      FROM ins JOIN policies AS p ON p.id = ins.policy_id
    RETURNING entity_id
    """
)

_SKIPPED_SQL = sa.text(
    """
    SELECT count(*)
      FROM policy_rules AS r JOIN policies AS p ON p.id = r.policy_id
     WHERE p.document_type IS NULL AND p.name = :name
       AND r.source_clause_ref = :ref
       AND (r.definition <> CAST(:definition AS jsonb)
            OR CAST(r.rule_type AS text) <> :rule_type)
    """
)

_DELETE_SQL = sa.text(
    """
    WITH del AS (
        DELETE FROM policy_rules AS r
         USING policies AS p
         WHERE p.id = r.policy_id
           AND CAST(r.rule_type AS text) = ANY(:rule_types)
        RETURNING r.id, p.organization_id, r.rule_type, r.applies_to, r.definition,
                  r.source_clause_ref, r.status
    )
    INSERT INTO audit_log
        (organization_id, actor_id, action, entity_type, entity_id, before_state, after_state)
    SELECT organization_id, NULL, 'policy_rule.deleted', 'policy_rule', id,
           jsonb_build_object(
               'rule_type', CAST(rule_type AS text),
               'applies_to', CAST(applies_to AS text),
               'source_clause_ref', source_clause_ref,
               'status', CAST(status AS text),
               'definition', definition),
           jsonb_build_object('source', CAST(:source AS text))
      FROM del
    RETURNING entity_id, before_state
    """
)

_USED_CATEGORIES_SQL = sa.text(
    "SELECT count(*) FROM expenses WHERE CAST(category AS text) = ANY(:categories)"
)


def apply_spend_rules(bind: Any) -> int:
    """Insert the six rules into every stub policy that lacks them (idempotent).

    *bind* is a SQLAlchemy connection.  Returns the number of rule rows inserted
    (each with one audit_log row).  Separate from upgrade() so it is testable.
    """
    inserted = 0
    for ref, rule_type, applies_to, definition in NEW_RULES:
        params = {
            "ref": ref,
            "rule_type": rule_type,
            "applies_to": applies_to,
            "definition": json.dumps(definition),
            "name": STUB_POLICY_NAME,
        }
        rows = bind.execute(_INSERT_SQL, {**params, "source": _SOURCE}).fetchall()
        inserted += len(rows)
        logger.info("0019: %s — %d rule row(s) inserted.", ref, len(rows))
        skipped = bind.execute(_SKIPPED_SQL, params).scalar()
        if skipped:
            logger.warning(
                "0019: %d stub-policy rule(s) already cite %s with a different definition "
                "and were left unchanged — review them in /policy.",
                skipped,
                ref,
            )
    return inserted


def remove_spend_rules(bind: Any) -> int:
    """Downgrade data step: refuse if new-category expenses exist, else delete the
    new-type rules (audited).  Returns the number of rule rows deleted."""
    used = bind.execute(_USED_CATEGORIES_SQL, {"categories": list(NEW_EXPENSE_CATEGORIES)}).scalar()
    if used:
        raise RuntimeError(
            f"0019 downgrade refused: {used} expense(s) use the mileage / entertainment / "
            "gift categories, which pre-0019 code cannot load. Re-categorise or remove "
            "them first."
        )
    frozen = {ref: definition for ref, _, _, definition in NEW_RULES}
    rows = bind.execute(
        _DELETE_SQL,
        {"rule_types": list(NEW_RULE_TYPES), "source": f"{_SOURCE} (downgrade)"},
    ).fetchall()
    for _entity_id, before in rows:
        before = before if isinstance(before, dict) else {}
        if frozen.get(before.get("source_clause_ref")) != before.get("definition"):
            logger.warning(
                "0019 downgrade: deleted a customised %s rule (definition kept in audit_log).",
                before.get("source_clause_ref"),
            )
    logger.info("0019 downgrade: %d rule row(s) deleted.", len(rows))
    return len(rows)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_EXPENSE_CATEGORIES:
            op.execute(f"ALTER TYPE expense_category ADD VALUE IF NOT EXISTS '{value}'")
        for value in NEW_RULE_TYPES:
            op.execute(f"ALTER TYPE policy_rule_type ADD VALUE IF NOT EXISTS '{value}'")
    op.add_column("expenses", sa.Column("attendee_count", sa.Integer(), nullable=True))
    op.add_column("expenses", sa.Column("disclosure", postgresql.JSONB(), nullable=True))
    op.add_column("expenses", sa.Column("mileage", postgresql.JSONB(), nullable=True))
    op.execute(
        "ALTER TABLE expenses ADD CONSTRAINT ck_expenses_attendee_count_positive "
        "CHECK (attendee_count IS NULL OR attendee_count >= 1) NOT VALID"
    )
    op.execute("ALTER TABLE expenses VALIDATE CONSTRAINT ck_expenses_attendee_count_positive")
    apply_spend_rules(op.get_bind())


def downgrade() -> None:
    remove_spend_rules(op.get_bind())
    op.execute("ALTER TABLE expenses DROP CONSTRAINT IF EXISTS ck_expenses_attendee_count_positive")
    op.drop_column("expenses", "mileage")
    op.drop_column("expenses", "disclosure")
    op.drop_column("expenses", "attendee_count")
