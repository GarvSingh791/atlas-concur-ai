"""Product gap 2 / 8 — entry-level rules end to end on Postgres.

* migration 0017: rewrites only canonical HOTEL-CAP / MEALS-PD rows, inserts the
  GRND-01 / 03 / 05 rules once per rule-bearing policy (never over an existing /
  archived one), audits every row, is idempotent and reverses cleanly;
* run_presubmit with the seeded rules: MEAL-02 on a multi-segment and a same-day
  approved trip, LOD-02 on a trip hotel vs an unlinked one, MEAL-04 alcohol read from
  receipt line items, GRND-01 / GRND-03 / GRND-05 on classified ground lines;
* GET /policy/rules/{id}/history: finance + managers read, employees 403, other org 404,
  whitelisted before -> after changes only.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import PreSubmitFlag, run_presubmit
from ai_agents.context import AgentContext
from backend.app.core.audit import record_audit
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.identity import Organization, User
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
from database.seeds.policy_content import ENTRY_RULES, RULES

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIG_0017 = _REPO_ROOT / "database" / "migrations" / "versions" / "0017_entry_level_policy_rules.py"
API = "/api/v1"


def _mig() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0017_db", _MIG_0017)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rules_of(db: Session, policy_id: uuid.UUID, ref: str) -> list[PolicyRule]:
    return list(
        db.scalars(
            sa.select(PolicyRule).where(
                PolicyRule.policy_id == policy_id, PolicyRule.source_clause_ref == ref
            )
        ).all()
    )


def _definition(db: Session, rule_id: uuid.UUID) -> dict:
    rule = db.get(PolicyRule, rule_id)
    assert rule is not None
    return rule.definition


def _audit(db: Session, rule_id: uuid.UUID) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog)
            .where(AuditLog.entity_type == "policy_rule", AuditLog.entity_id == rule_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
    )


# ---------------------------------------------------------------------------
# Migration 0017
# ---------------------------------------------------------------------------


class TestMigration0017:
    def _policy(self, db: Session, org: Organization, *, hotel: dict | None) -> Policy:
        mig = _mig()
        policy = f.make_policy(db, org=org, name=f"Rules {uuid.uuid4().hex[:6]}")
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.hotel,
            definition=json.loads(json.dumps(hotel if hotel is not None else mig.OLD_HOTEL_CAP)),
            source_clause_ref="HOTEL-CAP",
        )
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.per_diem,
            applies_to=PolicyAppliesTo.meals,
            definition=json.loads(json.dumps(mig.OLD_MEALS_PD)),
            source_clause_ref="MEALS-PD",
        )
        return policy

    def test_upgrade_idempotent_downgrade(self, db_session: Session, org_a: Organization) -> None:
        mig = _mig()
        policy = self._policy(db_session, org_a, hotel=None)
        edited = {**mig.OLD_HOTEL_CAP, "exception_approver": "VP"}
        admin = self._policy(db_session, org_a, hotel=edited)
        # An admin archived their own GRND-03 rule: it must never be re-added or touched.
        archived = f.make_policy_rule(
            db_session,
            policy=admin,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.all,
            definition={"note": "admin's own"},
            source_clause_ref="GRND-03",
            status=PolicyRuleStatus.archived,
        )
        no_hotel = f.make_policy(db_session, org=org_a, name="Corpus-like doc")
        conn = db_session.connection()

        counts = mig.apply_entry_level_rules(conn, forward=True)
        db_session.expire_all()
        assert counts["rewritten"] >= 2 and counts["inserted"] >= 5

        (hotel,) = _rules_of(db_session, policy.id, "HOTEL-CAP")
        assert hotel.definition == mig.NEW_HOTEL_CAP
        (meals,) = _rules_of(db_session, policy.id, "MEALS-PD")
        assert meals.definition == mig.NEW_MEALS_PD
        (admin_hotel,) = _rules_of(db_session, admin.id, "HOTEL-CAP")
        assert admin_hotel.definition == edited  # admin-changed: untouched
        for ref, rule_type, applies_to, definition in mig.NEW_RULES:
            (row,) = _rules_of(db_session, policy.id, ref)
            assert row.definition == definition and row.status == PolicyRuleStatus.active
            assert row.rule_type.value == rule_type and row.applies_to.value == applies_to
            (audit,) = _audit(db_session, row.id)
            assert audit.action == "policy_rule.seeded" and audit.actor_id is None
            assert audit.organization_id == org_a.id
            assert (audit.after_state or {})["definition"] == definition
        assert [r.id for r in _rules_of(db_session, admin.id, "GRND-03")] == [archived.id]
        assert _definition(db_session, archived.id) == {"note": "admin's own"}
        assert len(_rules_of(db_session, admin.id, "GRND-01")) == 1
        assert not _rules_of(db_session, no_hotel.id, "GRND-01")
        (hotel_audit,) = _audit(db_session, hotel.id)
        assert hotel_audit.action == "policy_rule.definition_updated"
        assert hotel_audit.before_state == {"definition": mig.OLD_HOTEL_CAP}

        # Idempotent.
        mig.apply_entry_level_rules(conn, forward=True)
        db_session.expire_all()
        for ref, *_ in mig.NEW_RULES:
            assert len(_rules_of(db_session, policy.id, ref)) == 1
        assert len(_audit(db_session, hotel.id)) == 1

        # Downgrade: inserted rows removed (audited), definitions restored.
        mig.apply_entry_level_rules(conn, forward=False)
        db_session.expire_all()
        for ref, *_ in mig.NEW_RULES:
            assert not [r for r in _rules_of(db_session, policy.id, ref)]
        assert _definition(db_session, hotel.id) == mig.OLD_HOTEL_CAP
        assert _definition(db_session, meals.id) == mig.OLD_MEALS_PD
        assert db_session.get(PolicyRule, archived.id) is not None  # never ours
        unseeded = db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.action == "policy_rule.unseeded", AuditLog.organization_id == org_a.id
            )
        ).all()
        assert len(unseeded) >= 3

    def test_a_changed_seeded_rule_survives_downgrade(
        self, db_session: Session, org_a: Organization
    ) -> None:
        mig = _mig()
        policy = self._policy(db_session, org_a, hotel=None)
        conn = db_session.connection()
        mig.apply_entry_level_rules(conn, forward=True)
        db_session.expire_all()
        (tips,) = _rules_of(db_session, policy.id, "GRND-05")
        tips.definition = {**tips.definition, "max_tip_pct": 15}
        db_session.flush()
        mig.apply_entry_level_rules(conn, forward=False)
        db_session.expire_all()
        assert db_session.get(PolicyRule, tips.id) is not None


# ---------------------------------------------------------------------------
# Presubmit with the seeded rules
# ---------------------------------------------------------------------------


def _seed_rules(db: Session, org: Organization) -> Policy:
    policy = f.make_policy(db, org=org, is_active=True)
    for r in (*RULES, *ENTRY_RULES):
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
    return policy


def _approved_trip(db: Session, emp: User, start: date, end: date) -> Trip:
    trip = f.make_trip(
        db,
        traveler=emp,
        start_date=start,
        end_date=end,
        status=TripStatus.approved,
        primary_destination="New York",
    )
    f.make_approval(
        db,
        submitter=emp,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.approved,
    )
    return trip


def _dt(d: date, hour: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _segment(
    db: Session, trip: Trip, kind: SegmentType, start: datetime, end: datetime, **kw: Any
) -> TripSegment:
    return f.make_trip_segment(
        db,
        trip=trip,
        segment_type=kind,
        start_datetime=start,
        end_datetime=end,
        location=kw.pop("location", "New York, NY"),
        **kw,
    )


def _planner_booked(db: Session, trip: Trip, segment: TripSegment) -> None:
    """Make *segment* a hotel the trip planner booked (review round 4): the trip carries
    the server-set planner recommendation_context and the segment names its option."""
    option_id = f"hotel-opt-{segment.id.hex[:8]}"
    segment.details = {**(segment.details or {}), "option_id": option_id}
    trip.recommendation_context = {
        "agent_task": "trip_planning",
        "selected_option_ids": [option_id],
        "selected_options": [{"option_id": option_id}],
        "reflection_outcome": "pass",
    }
    db.flush()


def _entry(
    category: str,
    amount: float,
    *,
    vendor: str | None = None,
    description: str | None = None,
    segment: TripSegment | None = None,
    receipt_id: uuid.UUID | None = None,
) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-03-02",
        vendor=vendor,
        description=description,
        status="matched",
        match_source=None,
        trip_segment_id=str(segment.id) if segment else None,
        is_itemized=False,
        primary_receipt_id=str(receipt_id) if receipt_id else None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def _run(db: Session, org: Organization, emp: User, *entries: DraftEntry) -> list[PreSubmitFlag]:
    ctx = AgentContext(organization_id=org.id, user_id=emp.id, granted_permission_keys=frozenset())
    report = DraftReport(
        entries=list(entries),
        total_amount=sum(e.amount for e in entries),
        currency="USD",
        trip_id=None,
        title="t",
    )
    flags, _ = run_presubmit(report, context=ctx, session=db, owner_id=emp.id)
    return flags


def _by_clause(flags: list[PreSubmitFlag], ref: str) -> list[PreSubmitFlag]:
    return [x for x in flags if x.source_clause_ref == ref]


class TestPresubmitEntryRules:
    def test_meal02_multi_segment_trip(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 3, 2), date(2026, 3, 4)
        trip = _approved_trip(db_session, emp, d1, d3)
        flight = _segment(db_session, trip, SegmentType.flight, _dt(d1, 8), _dt(d1, 11))
        hotel = _segment(db_session, trip, SegmentType.hotel, _dt(d1, 15), _dt(d3, 11))
        # 3 covered days (overlap counted once), NYC Tier 1: 85 + 2 x 63.75 = 212.50.
        ok = _run(
            db_session,
            org_a,
            emp,
            _entry("meals", 100.0, vendor="Diner", segment=flight),
            _entry("meals", 112.5, vendor="Bistro", segment=hotel),
        )
        assert not [x for x in ok if x.flag_type == "policy_violation"], [x.reason for x in ok]
        over = _run(
            db_session,
            org_a,
            emp,
            _entry("meals", 100.0, vendor="Diner", segment=flight),
            _entry("meals", 112.51, vendor="Bistro", segment=hotel),
        )
        violations = [x for x in over if x.flag_type == "policy_violation"]
        assert violations and all(x.source_clause_ref == "MEAL-02" for x in violations)
        assert violations[0].observed["travel_days"] == 2

    def test_meal02_same_day_trip(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d = date(2026, 3, 9)
        trip = _approved_trip(db_session, emp, d, d)
        seg = _segment(db_session, trip, SegmentType.flight, _dt(d, 7), _dt(d, 9))
        within = _run(db_session, org_a, emp, _entry("meals", 63.75, vendor="Cafe", segment=seg))
        assert not [x for x in within if x.flag_type == "policy_violation"]
        over = _run(db_session, org_a, emp, _entry("meals", 64.0, vendor="Cafe", segment=seg))
        (pv,) = [x for x in over if x.flag_type == "policy_violation"]
        assert pv.source_clause_ref == "MEAL-02" and "same-day trip" in pv.reason

    def test_lod02_trip_hotel_vs_unlinked(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 4, 6), date(2026, 4, 8)
        trip = _approved_trip(db_session, emp, d1, d3)
        hotel = _segment(
            db_session,
            trip,
            SegmentType.hotel,
            _dt(d1, 15),
            _dt(d3, 11),
            details={"nights": 2},
            supplier="Hilton",
        )
        # Review round 4: the segment must be one the PLANNER booked (before: any hotel
        # segment of an approved trip — see test_pg2_review_fixes_r4_db for the negative).
        _planner_booked(db_session, trip, hotel)
        # Security review round 3: the tool booking needs a receipt naming the booked
        # hotel in the booked city (before: a link to the hotel segment alone sufficed).
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": 660.0, "vendor": "HILTON", "location": "New York, NY"},
            field_provenance={
                "amount": "deterministic",
                "vendor": "deterministic",
                "location": "deterministic",
            },
        )
        linked = _run(
            db_session,
            org_a,
            emp,
            _entry("lodging", 660.0, vendor="Hilton", segment=hotel, receipt_id=receipt.id),
        )
        (adv,) = [x for x in linked if x.flag_type == "policy_advisory"]
        assert adv.source_clause_ref == "LOD-02" and adv.severity == "advisory"
        assert not _by_clause(linked, "HOTEL-CAP")

    @pytest.mark.parametrize("kind", [SegmentType.car, SegmentType.flight, SegmentType.rail])
    def test_lod02_needs_a_hotel_segment(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        kind: SegmentType,
    ) -> None:
        """A lodging entry linked to a car / flight / rail segment of an approved trip
        is NOT a stay booked through the travel tool: 13% over the Tier 1 cap stays a
        manager HOTEL-CAP exception, never a non-escalating LOD-02 advisory."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 4, 6), date(2026, 4, 8)
        trip = _approved_trip(db_session, emp, d1, d3)
        seg = _segment(db_session, trip, kind, _dt(d1, 15), _dt(d3, 11), details={"nights": 2})
        flags = _run(
            db_session, org_a, emp, _entry("lodging", 680.0, vendor="Some Indie Hotel", segment=seg)
        )
        (cap,) = _by_clause(flags, "HOTEL-CAP")
        assert cap.flag_type == "policy_violation", [x.reason for x in flags]
        assert cap.requires_approver == "manager"
        assert not [x for x in flags if x.flag_type == "policy_advisory"]

    def test_lod02_receipt_vendor_contradicting_the_booked_hotel(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """The trip booked a Hilton; the receipt READ a different hotel.  That stay was
        not the tool booking, so LOD-02's tolerance does not apply."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 4, 6), date(2026, 4, 8)
        trip = _approved_trip(db_session, emp, d1, d3)
        hotel = _segment(
            db_session,
            trip,
            SegmentType.hotel,
            _dt(d1, 15),
            _dt(d3, 11),
            details={"nights": 2},
            supplier="Hilton",
        )
        _planner_booked(db_session, trip, hotel)  # review round 4: a planner booking

        def _receipt(vendor: str, provenance: str) -> uuid.UUID:
            return f.make_receipt(
                db_session,
                uploaded_by=emp,
                ocr_status=OcrStatus.processed,
                extracted_fields={"amount": 680.0, "vendor": vendor, "location": "New York, NY"},
                field_provenance={
                    "amount": "deterministic",
                    "vendor": provenance,
                    "location": "deterministic",
                },
            ).id

        same = _run(
            db_session,
            org_a,
            emp,
            _entry(
                "lodging",
                680.0,
                vendor="Hilton",
                segment=hotel,
                receipt_id=_receipt("HILTON", "deterministic"),
            ),
        )
        assert _by_clause(same, "LOD-02") and not _by_clause(same, "HOTEL-CAP")
        contradicting = (("Some Indie Hotel", "deterministic"), ("Hilton", "user_edited"))
        for vendor, provenance in contradicting:
            flags = _run(
                db_session,
                org_a,
                emp,
                _entry(
                    "lodging",
                    680.0,
                    vendor="Hilton",
                    segment=hotel,
                    receipt_id=_receipt(vendor, provenance),
                ),
            )
            (cap,) = _by_clause(flags, "HOTEL-CAP")
            assert cap.flag_type == "policy_violation", (vendor, [x.reason for x in flags])
            assert cap.requires_approver == "manager"
            assert not _by_clause(flags, "LOD-02")

    @pytest.mark.parametrize(
        "receipt",
        [
            None,  # no receipt / card at all: nothing shows this is the booked stay
            {"vendor": ("HILTON", "deterministic")},  # no city read
            {"vendor": ("HILTON", "deterministic"), "location": ("Boston, MA", "deterministic")},
            {"vendor": ("HILTON", "deterministic"), "location": ("New York, NY", "user_edited")},
        ],
        ids=["no-evidence", "no-city", "other-city", "edited-city"],
    )
    def test_lod02_needs_the_booked_hotel_and_city_on_the_receipt(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        receipt: dict | None,
    ) -> None:
        """Security review round 3: a hotel segment alone is not proof of a tool booking —
        the receipt must name the booked hotel AND the booked city; otherwise 13% over the
        cap stays a manager HOTEL-CAP exception."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 4, 6), date(2026, 4, 8)
        trip = _approved_trip(db_session, emp, d1, d3)
        hotel = _segment(
            db_session,
            trip,
            SegmentType.hotel,
            _dt(d1, 15),
            _dt(d3, 11),
            details={"nights": 2},
            supplier="Hilton",
        )
        # Review round 4: a planner booking, so only the receipt evidence is missing.
        _planner_booked(db_session, trip, hotel)
        receipt_id = None
        if receipt is not None:
            fields: dict[str, Any] = {"amount": 680.0}
            prov: dict[str, Any] = {"amount": "deterministic"}
            for key, (value, source) in receipt.items():
                fields[key] = value
                prov[key] = source
            receipt_id = f.make_receipt(
                db_session,
                uploaded_by=emp,
                ocr_status=OcrStatus.processed,
                extracted_fields=fields,
                field_provenance=prov,
            ).id
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("lodging", 680.0, vendor="Hilton", segment=hotel, receipt_id=receipt_id),
        )
        (cap,) = _by_clause(flags, "HOTEL-CAP")
        assert cap.flag_type == "policy_violation", [x.reason for x in flags]
        assert cap.requires_approver == "manager"
        assert not [x for x in flags if x.flag_type == "policy_advisory"]

    def test_alcohol_line_items_from_receipt(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": 80.0,
                "vendor": "Steakhouse",
                "line_items": [
                    {"description": "Ribeye", "amount": 50.0},
                    {"description": "2x IPA beer", "amount": 30.0},
                ],
            },
            field_provenance={"amount": "deterministic", "vendor": "deterministic"},
        )
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("meals", 80.0, vendor="Steakhouse", receipt_id=receipt.id),
        )
        (pv,) = _by_clause(flags, "MEAL-04")
        assert pv.flag_type == "policy_violation" and pv.justifiable
        assert pv.observed["excluded_usd"] == 30.0

    def test_partial_claims_of_a_receipt(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Review round 3, end to end through build_entry_eval_item: the claimant claims
        the steak (85) of a steak + wine (105) receipt — the per-diem overage shows, and no
        MEAL-04 flag; a fare-only rideshare claim with a 20% tip is within GRND-05."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        meal = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": 105.0,
                "vendor": "Steakhouse",
                "line_items": [
                    {"description": "Steak dinner", "amount": 85.0},
                    {"description": "Glass of wine", "amount": 20.0},
                ],
            },
            field_provenance={"amount": "deterministic", "vendor": "deterministic"},
        )
        ride = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": 60.0, "vendor": "Uber", "tip": 10.0},
            field_provenance={"amount": "deterministic", "vendor": "deterministic"},
        )
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("meals", 85.0, vendor="Steakhouse", receipt_id=meal.id),
            _entry("ground_transport", 50.0, vendor="Uber", receipt_id=ride.id),
        )
        assert _by_clause(flags, "MEAL-04") == []
        (pd,) = [x for x in flags if x.flag_type == "policy_violation"]
        assert pd.observed["amount_usd"] == 85.0, pd.reason  # MEALS-PD: 85 > 65
        assert _by_clause(flags, "GRND-05") == []

    def test_ground_rules_on_classified_lines(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d9 = date(2026, 5, 1), date(2026, 5, 9)
        trip = _approved_trip(db_session, emp, d1, d9)
        rail = _segment(
            db_session,
            trip,
            SegmentType.rail,
            _dt(d1, 8),
            _dt(d1, 10),
            details={"class": "First"},
        )
        hotel = _segment(db_session, trip, SegmentType.hotel, _dt(d1, 15), _dt(d9, 11))
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("ground_transport", 180.0, vendor="Amtrak", segment=rail),
            _entry("ground_transport", 225.0, vendor="SFO Long Term Parking", segment=hotel),
            _entry("ground_transport", 30.0, vendor="Lyft", description="ride incl. tip"),
        )
        # Review round 5 (behaviour intentionally changed): the parking and Lyft lines
        # have no receipt / card / rail segment naming their kind, so every kind rule
        # reaches them as a could-not-verify; the Amtrak line (rail segment) is verified.
        grnd01 = _by_clause(flags, "GRND-01")
        (rail_flag,) = [x for x in grnd01 if x.flag_type == "policy_violation"]
        assert "under 3 hours" in rail_flag.reason
        assert rail_flag.justifiable
        assert len(grnd01) == 3
        assert all(
            x.flag_type == "not_evaluated" and x.observed.get("ground_subtype_unconfirmed")
            for x in grnd01
            if x is not rail_flag
        )
        # Review fix: the trip's 9 days are NOT parking days.  Airport parking with no
        # receipt-read day count is an honest "could not verify" advisory that names the
        # trip length as context only (behaviour intentionally changed).
        (park,) = [
            x for x in _by_clause(flags, "GRND-03") if x.observed.get("trip_days") is not None
        ]
        assert park.flag_type == "not_evaluated" and park.severity == "advisory"
        assert "parking_days" not in park.observed and park.observed["trip_days"] == 9
        assert "trip spans 9 days" in park.reason
        (tip,) = _by_clause(flags, "GRND-05")
        assert tip.flag_type == "not_evaluated"  # a tip mentioned, none itemized

    def test_parking_context_uses_the_trip_dates_not_the_segment_union(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Security review round 3: the trip-length context comes from the approved trip's
        start / end dates (9 days), not the union of its segments' dates (2 days here),
        and it never clears GRND-03 — the line stays a could-not-verify advisory."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d9 = date(2026, 5, 1), date(2026, 5, 9)
        trip = _approved_trip(db_session, emp, d1, d9)
        out = _segment(db_session, trip, SegmentType.flight, _dt(d1, 7), _dt(d1, 10))
        _segment(db_session, trip, SegmentType.flight, _dt(d9, 17), _dt(d9, 20))
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("ground_transport", 60.0, vendor="SFO Economy Parking", segment=out),
        )
        (park,) = _by_clause(flags, "GRND-03")
        assert park.flag_type == "not_evaluated" and park.severity == "advisory"
        assert park.observed["trip_days"] == 9 and "parking_days" not in park.observed

    def test_parking_on_a_long_trip_is_not_given_the_trip_length(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Hotel / city parking linked to a 9-day trip: GRND-03 (airport parking) never
        reaches it, and nothing turns the trip's length into parking days."""
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d9 = date(2026, 5, 1), date(2026, 5, 9)
        trip = _approved_trip(db_session, emp, d1, d9)
        _segment(db_session, trip, SegmentType.flight, _dt(d1, 7), _dt(d1, 10))
        hotel = _segment(db_session, trip, SegmentType.hotel, _dt(d1, 15), _dt(d9, 11))
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry(
                "ground_transport",
                25.0,
                vendor="Hilton parking",
                description="one night",
                segment=hotel,
            ),
            _entry(
                "ground_transport",
                40.0,
                vendor="Marriott Downtown",
                description="Hotel valet parking - client visit",
                segment=hotel,
            ),
            _entry(
                "ground_transport",
                18.0,
                vendor="City Garage",
                description="Hourly parking near client office",
                segment=hotel,
            ),
        )
        # Review round 5 (behaviour intentionally changed): typed in with no receipt,
        # nothing trusted says these are NOT airport parking, so GRND-03 reaches them —
        # only as a could-not-verify, never with the trip's length as parking days.
        for x in _by_clause(flags, "GRND-03"):
            assert x.flag_type == "not_evaluated"
            assert x.observed["could_not_verify"] == "airport_parking"
            assert "parking_days" not in x.observed
        assert not [x for x in flags if x.flag_type == "policy_violation"], [
            x.reason for x in flags
        ]

    def test_airport_parking_with_receipt_days_still_breaches(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": 90.0, "vendor": "SFO Long Term", "parking_days": 9},
            field_provenance={"amount": "deterministic", "vendor": "deterministic"},
        )
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("ground_transport", 90.0, vendor="SFO parking", receipt_id=receipt.id),
        )
        (park,) = _by_clause(flags, "GRND-03")
        assert park.flag_type == "policy_violation" and park.observed["parking_days"] == 9

    def test_typed_vendor_cannot_hide_a_rideshare_receipt(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """The receipt READ 'Uber'; the claimant typed 'Amtrak'.  The receipt's word
        still classifies the line (ambiguous -> broad), so the authored rideshare cap
        reaches it instead of only a non-escalating GRND-01 advisory."""
        emp = users_a["employee"]
        policy = _seed_rules(db_session, org_a)
        f.make_policy_rule(
            db_session,
            policy=policy,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.all,
            definition={
                "currency": "USD",
                "per": "item",
                "expense_categories": ["ground_transport"],
                "ground_subtypes": ["rideshare_taxi"],
                "tiers": [{"tier": "standard", "cap": 50}],
                "exception_approver": "manager",
            },
            source_clause_ref="RIDE-CAP",
            status=PolicyRuleStatus.active,
        )
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": 200.0, "vendor": "Uber"},
            field_provenance={"amount": "deterministic", "vendor": "deterministic"},
        )
        for typed in ("Uber", "Amtrak"):
            flags = _run(
                db_session,
                org_a,
                emp,
                _entry("ground_transport", 200.0, vendor=typed, receipt_id=receipt.id),
            )
            (cap,) = _by_clause(flags, "RIDE-CAP")
            assert cap.flag_type == "policy_violation", (typed, [x.reason for x in flags])
            # Security review round 3: typed words that DISAGREE with the receipt make
            # every named family a candidate, so the rail rule runs too (here an honest
            # could-not-verify on the class) — no rule is skipped.  A typed word that
            # agrees with the receipt verifies the line: no rail rule.
            rail = _by_clause(flags, "GRND-01")
            if typed == "Uber":
                assert rail == []
            else:
                assert [x.flag_type for x in rail] == ["not_evaluated"]


# ---------------------------------------------------------------------------
# GET /policy/rules/{id}/history
# ---------------------------------------------------------------------------


class TestRuleHistoryApi:
    def _rule_with_history(self, db: Session, org: Organization, actor: User) -> PolicyRule:
        policy = f.make_policy(db, org=org, is_active=True)
        rule = f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.receipt_required,
            applies_to=PolicyAppliesTo.all,
            definition={"threshold_usd": 25},
            source_clause_ref="RECEIPT-REQ",
            status=PolicyRuleStatus.draft,
        )
        drafted = record_audit(
            db,
            organization_id=org.id,
            action="policy_rule.drafted",
            entity_type="policy_rule",
            entity_id=rule.id,
            actor_id=actor.id,
            after_state={
                "status": "draft",
                "title": "Receipts required — $25 and over",
                "intent": "SECRET-ISH free text the history must not echo",
                "ai_decision_log_id": "abc",
            },
        )
        updated = record_audit(
            db,
            organization_id=org.id,
            action="policy_rule.definition_updated",
            entity_type="policy_rule",
            entity_id=rule.id,
            actor_id=None,
            before_state={"definition": {"threshold_usd": 25}},
            after_state={"definition": {"threshold_usd": 30}, "source": "migration X"},
        )
        # One test transaction gives every row the same now(): date the history explicitly
        # (earlier than the PATCH the test makes) so the order is deterministic.
        now = db.scalar(sa.select(sa.func.now()))
        assert now is not None
        drafted.created_at = now - timedelta(hours=2)
        updated.created_at = now - timedelta(hours=1)
        db.flush()
        return rule

    def test_finance_reads_whitelisted_changes(
        self, finance_client: Any, db_session: Session, org_a: Organization, users_a: Any
    ) -> None:
        rule = self._rule_with_history(db_session, org_a, users_a["finance"])
        r = finance_client.patch(f"{API}/policy-rules/{rule.id}", json={"status": "active"})
        assert r.status_code == 200, r.text

        r = finance_client.get(f"{API}/policy/rules/{rule.id}/history")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rule_id"] == str(rule.id)
        actions = [e["action"] for e in body["entries"]]
        assert actions == [
            "policy_rule.drafted",
            "policy_rule.definition_updated",
            "policy_rule.activated",
        ]
        drafted, updated, activated = body["entries"]
        assert drafted["actor_name"] == users_a["finance"].name
        assert {"field": "status", "before": None, "after": "draft"} in drafted["changes"]
        assert "SECRET-ISH" not in r.text and "ai_decision_log_id" not in r.text
        assert updated["actor_name"] is None and updated["note"] == "migration X"
        assert updated["changes"] == [
            {"field": "definition.threshold_usd", "before": 25, "after": 30}
        ]
        # Review fix: title / rule_type / applies_to are context on an activation row,
        # not changes — the ONLY change is the status.
        assert activated["changes"] == [{"field": "status", "before": "draft", "after": "active"}]

    def test_manager_may_read_employee_may_not(
        self,
        manager_client: Any,
        employee_client: Any,
        db_session: Session,
        org_a: Organization,
        users_a: Any,
    ) -> None:
        rule = self._rule_with_history(db_session, org_a, users_a["finance"])
        assert manager_client.get(f"{API}/policy/rules/{rule.id}/history").status_code == 200
        r = employee_client.get(f"{API}/policy/rules/{rule.id}/history")
        assert r.status_code == 403
        assert "error" in r.json()

    def test_other_org_rule_is_404(
        self, finance_client: Any, db_session: Session, org_b: Organization, users_b: Any
    ) -> None:
        rule = self._rule_with_history(db_session, org_b, users_b["finance"])
        r = finance_client.get(f"{API}/policy/rules/{rule.id}/history")
        assert r.status_code == 404
        assert finance_client.get(f"{API}/policy/rules/{uuid.uuid4()}/history").status_code == 404
