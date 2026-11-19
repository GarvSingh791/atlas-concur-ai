"""Canonical policy figures: HOTEL-CAP / MEALS-PD rules follow TEP-FY26 (Issue 6).

Revision ID: 0015_canonical_policy_figures
Revises: 0014_approval_step_snapshot
Create Date: 2026-11-11

DATA ONLY — no schema change.  The deterministic rule engine read the retired
8-clause stub's figures (HOTEL-CAP tier1 USD 250 / domestic 180; MEALS-PD
domestic 75 / international 100) while policy Q&A, the corpus and the published
Helios TEP-FY26 policy say Tier 1 USD 300 / Tier 2 225 / Tier 3 160 per night
(LOD-01) and USD 85 / 75 / 65 per day (MEAL-01).  This revision rewrites the
two ``policy_rules.definition`` rows to the canonical, city-tiered shape that
``database/seeds/policy_content.py`` derives from ``corpus/figures.py``.

Safety:
  * Only a row whose definition still EQUALS the frozen stub literal (jsonb
    equality — key order irrelevant) is rewritten; any other rule (e.g. one a
    finance user authored and activated) is left alone and reported at WARNING.
  * Rows are updated in place, so rule ids stay stable (stored / fresh flag
    de-duplication keys on rule_id).  Historical approval risk summaries and
    segment policy notes are append-only decision history and are NOT rewritten.
  * Every rewritten row gets an ``audit_log`` row (actor NULL = system) with
    the full before / after definition.
  * Tiers / rates are stored LOWEST FIRST (Tier 3 default first): pre-0015 code
    still serving traffic during a rolling deploy falls back to the first entry
    and therefore applies the most restrictive figure (fail closed).
  * The stub policy's ``document_text`` gets the matching [HOTEL-CAP] /
    [MEALS-PD] sections via replace() — a no-op when the text has drifted.
  * Idempotent; a fresh database is a no-op (the seed runs afterwards and writes
    the canonical values).  ``downgrade()`` reverses both, with its own audit rows.

The literals below are FROZEN copies — never import them from the seed module,
which may change after this revision.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0015_canonical_policy_figures"
down_revision: str | None = "0014_approval_step_snapshot"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")

STUB_POLICY_NAME = "Atlas Global Travel & Expense Policy"

OLD_HOTEL_CAP: dict[str, Any] = {
    "currency": "USD",
    "per": "night",
    "tiers": [
        {
            "tier": "tier1",
            "cities": ["New York", "San Francisco", "London", "Tokyo", "Paris", "Zurich"],
            "cap": 250,
        },
        {"tier": "domestic", "description": "All other US cities", "cap": 180},
        {"tier": "intl_high", "description": "International high-cost (non-Tier-1)", "cap": 300},
        {"tier": "intl_other", "description": "All other international", "cap": 200},
    ],
    "exception_approver": "manager",
}

NEW_HOTEL_CAP: dict[str, Any] = {
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
        {
            "tier": "tier2",
            "label": "Tier 2",
            "cities": [
                "Chicago",
                "Seattle",
                "Washington DC",
                "Toronto",
                "Sydney",
                "Dubai",
                "Frankfurt",
                "Amsterdam",
                "Dublin",
            ],
            "cap": 225,
        },
        {
            "tier": "tier1",
            "label": "Tier 1",
            "cities": [
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
            ],
            "aliases": {"New York": ["New York City", "NYC", "Manhattan"]},
            "cap": 300,
        },
    ],
    "exception_approver": "manager",
}

OLD_MEALS_PD: dict[str, Any] = {
    "currency": "USD",
    "per": "day",
    "rates": [{"region": "domestic", "amount": 75}, {"region": "international", "amount": 100}],
    "exclusions": ["alcohol"],
    "group_meal_threshold": 3,
    "group_meal_approver": "manager",
}

NEW_MEALS_PD: dict[str, Any] = {
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
        {
            "tier": "tier2",
            "label": "Tier 2",
            "cities": [
                "Chicago",
                "Seattle",
                "Washington DC",
                "Toronto",
                "Sydney",
                "Dubai",
                "Frankfurt",
                "Amsterdam",
                "Dublin",
            ],
            "amount": 75,
        },
        {
            "tier": "tier1",
            "label": "Tier 1",
            "cities": [
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
            ],
            "aliases": {"New York": ["New York City", "NYC", "Manhattan"]},
            "amount": 85,
        },
    ],
    "exclusions": ["alcohol"],
    "group_meal_threshold": 3,
    "group_meal_approver": "manager",
}

OLD_HOTEL_SECTION: str = (
    "[HOTEL-CAP] SECTION 3 — HOTEL NIGHTLY RATE CAPS\n"
    "Reimbursable nightly hotel rates (room rate only, excluding taxes) are capped as "
    "follows:\n"
    "  • Tier-1 cities (New York, San Francisco, London, Tokyo, Paris, Zurich): USD 250.\n"
    "  • Domestic (all other US cities): USD 180.\n"
    "  • International high-cost (excluding Tier-1): USD 300.\n"
    "  • All other international: USD 200.\n"
    "Rates exceeding the cap require manager pre-approval prior to booking.\n"
    "\n"
)

NEW_HOTEL_SECTION: str = (
    "[HOTEL-CAP] SECTION 3 — HOTEL NIGHTLY RATE CAPS\n"
    "Reimbursable nightly hotel rates (room + tax) are capped by city tier:\n"
    "  • Tier 1 (New York, San Francisco, London, Tokyo, Singapore, Hong Kong, Zurich, "
    "Geneva, Boston, Los Angeles, Paris): USD 300.\n"
    "  • Tier 2 (Chicago, Seattle, Washington DC, Toronto, Sydney, Dubai, Frankfurt, "
    "Amsterdam, Dublin): USD 225.\n"
    "  • Tier 3 (all other locations): USD 160.\n"
    "Rates exceeding the cap require manager pre-approval prior to booking.\n"
    "\n"
)

OLD_MEALS_SECTION: str = (
    "[MEALS-PD] SECTION 5 — MEAL AND INCIDENTALS PER DIEM\n"
    "Daily meal and incidental allowances:\n"
    "  • Domestic travel (within the United States): USD 75 per day.\n"
    "  • International travel: USD 100 per day.\n"
)

NEW_MEALS_SECTION: str = (
    "[MEALS-PD] SECTION 5 — MEAL AND INCIDENTALS PER DIEM\n"
    "Daily meal and incidental allowances by city tier:\n"
    "  • Tier 1 cities: USD 85 per day.\n"
    "  • Tier 2 cities: USD 75 per day.\n"
    "  • Tier 3 (all other locations): USD 65 per day.\n"
)

RULE_CHANGES: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("HOTEL-CAP", OLD_HOTEL_CAP, NEW_HOTEL_CAP),
    ("MEALS-PD", OLD_MEALS_PD, NEW_MEALS_PD),
)
_SOURCE = "migration 0015: canonical TEP-FY26 figures (LOD-01 / MEAL-01)"

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

_DOCUMENT_SQL = sa.text(
    """
    UPDATE policies
       SET document_text = replace(replace(document_text, :old_hotel, :new_hotel),
                                   :old_meals, :new_meals),
           updated_at = now()
     WHERE document_type IS NULL
       AND name = :name
       AND (position(:old_hotel IN document_text) > 0
            OR position(:old_meals IN document_text) > 0)
    """
)


def apply_canonical_figures(bind: Any, *, forward: bool) -> int:
    """Rewrite the stub HOTEL-CAP / MEALS-PD rules (forward) or restore them.

    *bind* is a SQLAlchemy connection.  Returns the number of rule rows rewritten
    (each with one audit_log row).  Separate from upgrade() so it is testable.
    """
    rewritten = 0
    action = "policy_rule.definition_updated" if forward else "policy_rule.definition_reverted"
    source = _SOURCE if forward else f"{_SOURCE} (downgrade)"
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
        rewritten += len(updated)
        logger.info("0015: %s — %d rule row(s) rewritten.", ref, len(updated))
        unmatched = bind.execute(
            _UNMATCHED_SQL, {"ref": ref, "old": json.dumps(old), "new": json.dumps(new)}
        ).scalar()
        if unmatched:
            logger.warning(
                "0015: %d active %s rule(s) match neither the stub nor the canonical "
                "definition and were left unchanged — review them in /policy.",
                unmatched,
                ref,
            )
    old_hotel, new_hotel = (
        (OLD_HOTEL_SECTION, NEW_HOTEL_SECTION)
        if forward
        else (NEW_HOTEL_SECTION, OLD_HOTEL_SECTION)
    )
    old_meals, new_meals = (
        (OLD_MEALS_SECTION, NEW_MEALS_SECTION)
        if forward
        else (NEW_MEALS_SECTION, OLD_MEALS_SECTION)
    )
    bind.execute(
        _DOCUMENT_SQL,
        {
            "old_hotel": old_hotel,
            "new_hotel": new_hotel,
            "old_meals": old_meals,
            "new_meals": new_meals,
            "name": STUB_POLICY_NAME,
        },
    )
    return rewritten


def upgrade() -> None:
    apply_canonical_figures(op.get_bind(), forward=True)


def downgrade() -> None:
    apply_canonical_figures(op.get_bind(), forward=False)
