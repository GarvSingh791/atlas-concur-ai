"""Issue 6 — DB-backed checks for the canonical policy figures.

* migration 0015 rewrites ONLY stub-equal HOTEL-CAP / MEALS-PD rows (in place,
  one audit_log row each), is idempotent, fixes the stub document sections and
  reverses cleanly; a separately authored rule is never touched;
* database/seeds/refresh_draft_presubmit_flags.py recomputes stale DRAFT
  snapshots ("$180") and leaves submitted reports alone;
* the planner persists a hotel segment the trip_segments CHECK accepts, even for
  a same-day trip, with nightly rate / nights / total;
* the seeded demo texts (Q3 report, Chicago trip) equal the engine's own output.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item,
    draft_entries_for_expenses,
    run_presubmit,
)
from ai_agents.agents.travel_agent.persistence import persist_trip_selection
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.context import AgentContext
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, User
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.trip_segment import SegmentType, TripSegment
from database.seeds.policy_content import RULES

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT / "database" / "migrations" / "versions" / "0015_canonical_policy_figures.py"
)


def _mig() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0015_db", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_rules(db: Session, policy: Policy) -> None:
    for r in RULES:
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )


def _definition(db: Session, rule_id: uuid.UUID) -> dict[str, Any]:
    rule = db.get(PolicyRule, rule_id)
    assert rule is not None
    return rule.definition


def _document(db: Session, policy_id: uuid.UUID) -> str:
    policy = db.get(Policy, policy_id)
    assert policy is not None and policy.document_text is not None
    return policy.document_text


def _snapshot(db: Session, report_id: uuid.UUID) -> dict[str, Any]:
    report = db.get(ExpenseReport, report_id)
    assert report is not None and isinstance(report.pre_submit_flags, dict)
    return report.pre_submit_flags


# ---------------------------------------------------------------------------
# Migration 0015
# ---------------------------------------------------------------------------


class TestMigration0015Apply:
    def _stub(
        self, db: Session, org: Organization
    ) -> tuple[Policy, PolicyRule, PolicyRule, PolicyRule]:
        mig = _mig()
        policy = f.make_policy(
            db,
            org=org,
            name=mig.STUB_POLICY_NAME,
            is_active=False,
            document_text=f"HEAD\n\n{mig.OLD_HOTEL_SECTION}MIDDLE\n\n{mig.OLD_MEALS_SECTION}TAIL",
        )
        hotel = f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.hotel,
            definition=json.loads(json.dumps(mig.OLD_HOTEL_CAP)),
            source_clause_ref="HOTEL-CAP",
        )
        meals = f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.per_diem,
            applies_to=PolicyAppliesTo.meals,
            definition=json.loads(json.dumps(mig.OLD_MEALS_PD)),
            source_clause_ref="MEALS-PD",
        )
        authored = f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.hotel,
            definition={"currency": "USD", "tiers": [{"tier": "domestic", "cap": 199}]},
            source_clause_ref="HOTEL-CAP",
        )
        return policy, hotel, meals, authored

    def _audit(self, db: Session, rule_id: uuid.UUID) -> list[AuditLog]:
        return list(
            db.scalars(
                sa.select(AuditLog)
                .where(AuditLog.entity_type == "policy_rule", AuditLog.entity_id == rule_id)
                .order_by(AuditLog.created_at)
            ).all()
        )

    def test_upgrade_idempotent_downgrade(self, db_session: Session, org_a: Organization) -> None:
        mig = _mig()
        policy, hotel, meals, authored = self._stub(db_session, org_a)
        conn = db_session.connection()

        assert mig.apply_canonical_figures(conn, forward=True) >= 2
        db_session.expire_all()
        assert _definition(db_session, hotel.id) == mig.NEW_HOTEL_CAP
        assert _definition(db_session, meals.id) == mig.NEW_MEALS_PD
        assert _definition(db_session, authored.id)["tiers"][0]["cap"] == 199
        doc = _document(db_session, policy.id)
        assert mig.NEW_HOTEL_SECTION in doc and mig.NEW_MEALS_SECTION in doc
        assert doc.startswith("HEAD") and doc.endswith("TAIL") and "MIDDLE" in doc
        (row,) = self._audit(db_session, hotel.id)
        assert row.action == "policy_rule.definition_updated"
        assert row.actor_id is None and row.organization_id == org_a.id
        assert row.before_state == {"definition": mig.OLD_HOTEL_CAP}
        assert (row.after_state or {}).get("definition") == mig.NEW_HOTEL_CAP
        assert not self._audit(db_session, authored.id)

        # Idempotent: nothing stub-equal is left, no new audit rows for our rules.
        mig.apply_canonical_figures(conn, forward=True)
        db_session.expire_all()
        assert len(self._audit(db_session, hotel.id)) == 1

        mig.apply_canonical_figures(conn, forward=False)
        db_session.expire_all()
        assert _definition(db_session, hotel.id) == mig.OLD_HOTEL_CAP
        assert _definition(db_session, meals.id) == mig.OLD_MEALS_PD
        assert _definition(db_session, authored.id)["tiers"][0]["cap"] == 199
        doc = _document(db_session, policy.id)
        assert mig.OLD_HOTEL_SECTION in doc and mig.OLD_MEALS_SECTION in doc
        actions = [r.action for r in self._audit(db_session, hotel.id)]
        assert actions == ["policy_rule.definition_updated", "policy_rule.definition_reverted"]


# ---------------------------------------------------------------------------
# Draft pre-submit refresh
# ---------------------------------------------------------------------------


_STALE = {
    "flags": [
        {
            "entry_ref": "x",
            "flag_type": "policy_violation",
            "source_clause_ref": "HOTEL-CAP",
            "reason": "Hotel amount 750.00 USD exceeds the domestic cap 180.00 USD per night.",
            "severity": "blocking",
        }
    ],
    "blocking_count": 1,
    "advisory_count": 0,
}


def test_refresh_draft_presubmit_flags(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    from database.seeds.refresh_draft_presubmit_flags import refresh

    _seed_rules(db_session, f.make_policy(db_session, org=org_a, is_active=True))
    emp = users_a["employee"]
    draft = f.make_expense_report(db_session, owner=emp, pre_submit_flags=_STALE)
    f.make_expense(
        db_session,
        report=draft,
        category=ExpenseCategory.lodging,
        amount="750.00",
        vendor="Marriott Marquis New York",
        description="Hotel - 3 nights @ 250.00/night",
        expense_date=date(2026, 6, 10),
    )
    submitted = f.make_expense_report(
        db_session, owner=emp, status=ExpenseReportStatus.submitted, pre_submit_flags=_STALE
    )

    @contextlib.contextmanager
    def _factory() -> Iterator[Session]:
        yield db_session

    result = refresh(session_factory=_factory)
    assert result["failed"] == 0 and result["refreshed"] >= 1
    db_session.expire_all()
    flags = _snapshot(db_session, draft.id)["flags"]
    cap = [fl for fl in flags if fl.get("source_clause_ref") == "HOTEL-CAP"]
    assert cap and "180" not in cap[0]["reason"]
    assert "within the Tier 1 New York cap of $300/night" in cap[0]["reason"]
    assert cap[0]["pending_verification"] == "nights_and_city"
    assert _snapshot(db_session, submitted.id) == _STALE


# ---------------------------------------------------------------------------
# Planner persistence
# ---------------------------------------------------------------------------


def _hotel_option(rate: float, nights: int | None) -> RankedOption:
    return RankedOption(
        option_id="NYC-MARRIOTT-SYN",
        segment_type="hotel",
        supplier="Marriott International",
        amount_usd=rate,
        sustainability_score=2,
        cabin_class=None,
        duration_hours=None,
        is_direct=None,
        city="New York",
        nightly_rate_usd=rate,
        room_type="Standard",
        in_policy=True,
        policy_reason="ok",
        clause_refs=("HOTEL-CAP",),
        requires_approver=None,
        rule_in_policy=True,
        rank_score=1.0,
        explanation="x",
        nights=nights,
        stay_total_usd=rate * nights if nights else None,
    )


@pytest.mark.parametrize(
    ("dep", "ret", "nights"),
    [("2026-11-17", "2026-11-20", 3), ("2026-11-17", "2026-11-17", 1), ("2026-11-17", None, 1)],
)
def test_planner_hotel_segment_persists(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    dep: str,
    ret: str | None,
    nights: int,
) -> None:
    emp = users_a["employee"]
    ctx = AgentContext(
        organization_id=org_a.id, user_id=emp.id, granted_permission_keys=frozenset()
    )
    intent = SimpleNamespace(
        departure_date=dep,
        return_date=ret,
        origin="SFO",
        destination="New York",
        currency="USD",
        purpose="Client visit",
    )
    opt = _hotel_option(258.0, nights)
    trip = persist_trip_selection(
        intent=intent,
        shortlist=[opt],
        selected_ids=[opt.option_id],
        reflection_outcome_value=None,
        context=ctx,
        session=db_session,
    )
    seg = db_session.scalars(sa.select(TripSegment).where(TripSegment.trip_id == trip.id)).one()
    assert seg.segment_type == SegmentType.hotel
    assert (seg.details or {}).get("nights") == nights
    assert seg.amount == Decimal(str(258.0 * nights)).quantize(Decimal("0.01"))
    assert trip.estimated_cost == seg.amount
    start, end = seg.start_datetime, seg.end_datetime
    assert isinstance(start, datetime) and isinstance(end, datetime)
    assert end > start


# ---------------------------------------------------------------------------
# Seed coherence — seeded demo texts == engine output
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_demo() -> dict[str, Any]:
    from backend.app.core.database import get_engine, get_sessionmaker

    try:
        with get_engine().connect() as probe:
            probe.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres not reachable")
    from database.seeds.seed_all import seed

    seed()
    return {"factory": get_sessionmaker()}


def test_seeded_q3_and_chicago_texts_match_the_engine(seeded_demo: dict[str, Any]) -> None:
    from database.seeds import seed_all

    with seeded_demo["factory"]() as session:
        org = session.scalars(
            sa.select(Organization).where(Organization.name == "Atlas Demo Corp")
        ).one()
        report = session.scalars(
            sa.select(ExpenseReport).where(
                ExpenseReport.organization_id == org.id,
                ExpenseReport.title == "Q3 Conference & Travel",
            )
        ).one()
        entries = list(
            session.scalars(
                sa.select(Expense)
                .where(Expense.report_id == report.id)
                .order_by(Expense.created_at, Expense.id)
            ).all()
        )
        ctx = AgentContext(
            organization_id=org.id, user_id=report.owner_id, granted_permission_keys=frozenset()
        )
        drafts = draft_entries_for_expenses(entries, session=session, organization_id=org.id)
        flags, _ = run_presubmit(
            DraftReport(
                entries=drafts,
                total_amount=float(report.total_amount),
                currency="USD",
                trip_id=None,
                title=report.title,
            ),
            context=ctx,
            session=session,
            owner_id=report.owner_id,
        )
        fresh = {
            fl.source_clause_ref: fl
            for fl in flags
            if fl.source_clause_ref in ("HOTEL-CAP", "MEALS-PD")
        }
        stored = {
            fl["source_clause_ref"]: fl
            for fl in report.pre_submit_flags["flags"]
            if fl.get("source_clause_ref") in ("HOTEL-CAP", "MEALS-PD")
        }
        assert set(fresh) == set(stored) == {"HOTEL-CAP", "MEALS-PD"}
        for ref, fl in fresh.items():
            assert stored[ref]["reason"] == fl.reason, ref
            assert stored[ref]["severity"] == fl.severity, ref
            assert stored[ref]["pending_verification"] == fl.pending_verification, ref
            assert stored[ref]["observed"] == fl.observed, ref
        for text in (seed_all.Q3_HOTEL_REASON, seed_all.Q3_MEALS_REASON):
            assert "250.00 USD cap" not in text and "60.00 USD per-diem" not in text

        chicago = session.scalars(
            sa.select(TripSegment).where(
                TripSegment.location == "Chicago, IL",
                TripSegment.segment_type == SegmentType.hotel,
                TripSegment.policy_notes.is_not(None),
            )
        ).first()
        assert chicago is not None
        entry = drafts[0].__class__(
            category="lodging",
            amount=float(chicago.amount),
            currency="USD",
            expense_date="2026-07-15",
            vendor=chicago.supplier,
            description=None,
            status="planned",
            match_source=None,
            trip_segment_id=str(chicago.id),
            is_itemized=False,
            primary_receipt_id=None,
            source_card_txn_id=None,
            sibling_group_id=None,
        )
        (verdict,) = PolicyEvalTool().evaluate_many(
            [build_eval_item(entry, segment=chicago)], context=ctx, session=session
        )
        cap = next(v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP")
        assert chicago.policy_notes == cap.reason == seed_all.CHICAGO_HOTEL_REASON
