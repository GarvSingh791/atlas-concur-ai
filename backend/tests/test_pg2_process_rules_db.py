"""Product gap 2, stage 2 — process & timing rules end to end on Postgres.

* migration 0018: the nullable ``expenses.receipt_affidavit_at`` column; RECEIPT-REQ /
  AIR-CABIN rewritten only when canonical; EXP-01 / PTA-02 / PTA-05 / AIR-06 / CARD-03
  inserted once per rule-bearing policy (never over an existing / archived one),
  audited, idempotent, reversible;
* EXP-01 (late advisory / stale VP approval) and CARD-03 (overdue card transactions) on
  the pre-submit check, at approval (a stale report never auto-approves) and at read
  time (an untouched draft shows it is stale);
* EXP-04: PATCH ``receipt_affidavit`` signs the affidavit (audited); the clause's
  amount ceiling and per-quarter count, owner-scoped;
* PTA-02 / AIR-06 / AIR-04 / PTA-05 at trip approval (escalation, clause-cited) and in
  the trip planner (notices at selection time, a change of an approved trip is linked
  and its re-approval named).
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents import process_checks
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import PreSubmitFlag, run_presubmit
from ai_agents.context import AgentContext
from backend.tests.test_uat2_planner_single_trip_db import (  # noqa: F401
    _client,
    _Planner,
    planner_app,
)
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
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
from database.models.trip_segment import SegmentType
from database.seeds.policy_content import ENTRY_RULES, PROCESS_RULES, RULES

pytestmark = pytest.mark.db

API = "/api/v1"
TODAY = date(2026, 11, 14)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIG = _REPO_ROOT / "database" / "migrations" / "versions" / "0018_process_timing_rules.py"


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


def _mig() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0018_db", _MIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(db: Session, org: Organization, *, process: bool = True) -> Policy:
    policy = f.make_policy(db, org=org, is_active=True)
    for r in (*RULES, *ENTRY_RULES, *(PROCESS_RULES if process else ())):
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=json.loads(json.dumps(r["definition"])),
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
    return policy


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id, user_id=user.id, granted_permission_keys=frozenset()
    )


def _draft(category: str, amount: float, day: date, **kw: Any) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date=day.isoformat(),
        vendor=kw.pop("vendor", "Corner Cafe"),
        description=kw.pop("description", None),
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=kw.pop("primary_receipt_id", None),
        source_card_txn_id=None,
        sibling_group_id=None,
        **kw,
    )


def _presubmit(
    db: Session, emp: User, *entries: DraftEntry, trip_id: Any = None
) -> list[PreSubmitFlag]:
    report = DraftReport(
        entries=list(entries),
        total_amount=sum(e.amount for e in entries),
        currency="USD",
        trip_id=str(trip_id) if trip_id else None,
        title="t",
    )
    flags, _ = run_presubmit(report, context=_ctx(emp), session=db, owner_id=emp.id)
    return flags


def _iso(value: object) -> str:
    assert isinstance(value, datetime)
    return value.isoformat()


def _clause(flags: list[Any], ref: str) -> list[Any]:
    return [
        x
        for x in flags
        if (x.get("source_clause_ref") if isinstance(x, dict) else x.source_clause_ref) == ref
    ]


# ---------------------------------------------------------------------------
# Migration 0018
# ---------------------------------------------------------------------------


class TestMigration0018:
    def _policy(self, db: Session, org: Organization, *, receipt: dict | None = None) -> Policy:
        mig = _mig()
        policy = f.make_policy(db, org=org, name=f"Rules {uuid.uuid4().hex[:6]}")
        for ref, rt, at, definition in (
            ("HOTEL-CAP", PolicyRuleType.amount_cap, PolicyAppliesTo.hotel, {"note": "cap"}),
            (
                "RECEIPT-REQ",
                PolicyRuleType.receipt_required,
                PolicyAppliesTo.all,
                receipt if receipt is not None else mig.OLD_RECEIPT_REQ,
            ),
            ("AIR-CABIN", PolicyRuleType.cabin_class, PolicyAppliesTo.flight, mig.OLD_AIR_CABIN),
        ):
            f.make_policy_rule(
                db,
                policy=policy,
                rule_type=rt,
                applies_to=at,
                definition=json.loads(json.dumps(definition)),
                source_clause_ref=ref,
            )
        return policy

    def _rules(self, db: Session, policy: Policy, ref: str) -> list[PolicyRule]:
        return list(
            db.scalars(
                sa.select(PolicyRule).where(
                    PolicyRule.policy_id == policy.id, PolicyRule.source_clause_ref == ref
                )
            ).all()
        )

    def test_column_is_nullable_timestamptz(self, db_session: Session) -> None:
        row = db_session.execute(
            sa.text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'expenses' AND column_name = 'receipt_affidavit_at'"
            )
        ).one()
        assert row == ("timestamp with time zone", "YES")

    def test_upgrade_idempotent_downgrade(self, db_session: Session, org_a: Organization) -> None:
        mig = _mig()
        policy = self._policy(db_session, org_a)
        edited = {**mig.OLD_RECEIPT_REQ, "threshold_usd": 40}
        admin = self._policy(db_session, org_a, receipt=edited)
        archived = f.make_policy_rule(
            db_session,
            policy=admin,
            rule_type=PolicyRuleType.category_allow,
            applies_to=PolicyAppliesTo.all,
            definition={"note": "admin's own deadline"},
            source_clause_ref="EXP-01",
            status=PolicyRuleStatus.archived,
        )
        no_hotel = f.make_policy(db_session, org=org_a, name="Corpus-like doc")
        conn = db_session.connection()

        counts = mig.apply_process_rules(conn, forward=True)
        db_session.expire_all()
        assert counts["rewritten"] >= 3 and counts["inserted"] >= 9

        (receipt,) = self._rules(db_session, policy, "RECEIPT-REQ")
        assert receipt.definition == mig.NEW_RECEIPT_REQ
        (cabin,) = self._rules(db_session, policy, "AIR-CABIN")
        assert cabin.definition == mig.NEW_AIR_CABIN
        (admin_receipt,) = self._rules(db_session, admin, "RECEIPT-REQ")
        assert admin_receipt.definition == edited  # admin-changed: untouched
        for ref, rule_type, applies_to, definition in mig.NEW_RULES:
            (row,) = self._rules(db_session, policy, ref)
            assert row.definition == definition and row.status == PolicyRuleStatus.active
            assert row.rule_type.value == rule_type and row.applies_to.value == applies_to
            audits = db_session.scalars(
                sa.select(AuditLog).where(AuditLog.entity_id == row.id)
            ).all()
            assert [a.action for a in audits] == ["policy_rule.seeded"]
            assert audits[0].actor_id is None and audits[0].organization_id == org_a.id
        assert [r.id for r in self._rules(db_session, admin, "EXP-01")] == [archived.id]
        assert len(self._rules(db_session, admin, "PTA-02")) == 1
        assert not self._rules(db_session, no_hotel, "EXP-01")

        # Idempotent.
        again = mig.apply_process_rules(conn, forward=True)
        db_session.expire_all()
        assert again["inserted"] == 0
        for ref, *_ in mig.NEW_RULES:
            assert len(self._rules(db_session, policy, ref)) == 1

        # Downgrade: inserted rows removed (audited), definitions restored.
        mig.apply_process_rules(conn, forward=False)
        db_session.expire_all()
        for ref, *_ in mig.NEW_RULES:
            assert not self._rules(db_session, policy, ref)
        assert self._rules(db_session, policy, "RECEIPT-REQ")[0].definition == mig.OLD_RECEIPT_REQ
        assert self._rules(db_session, policy, "AIR-CABIN")[0].definition == mig.OLD_AIR_CABIN
        assert db_session.get(PolicyRule, archived.id) is not None

    def test_seed_literals_match_the_migration(self) -> None:
        mig = _mig()
        seeded = {r["clause_ref"]: r for r in (*RULES, *PROCESS_RULES)}
        assert seeded["RECEIPT-REQ"]["definition"] == mig.NEW_RECEIPT_REQ
        assert seeded["AIR-CABIN"]["definition"] == mig.NEW_AIR_CABIN
        for ref, rule_type, applies_to, definition in mig.NEW_RULES:
            assert seeded[ref]["definition"] == definition
            assert (seeded[ref]["rule_type"], seeded[ref]["applies_to"]) == (rule_type, applies_to)


# ---------------------------------------------------------------------------
# EXP-01 — submission deadline / stale report
# ---------------------------------------------------------------------------


class TestSubmissionDeadline:
    @pytest.mark.parametrize(
        ("age", "flag_type", "approver"),
        [(30, None, None), (31, "late_submission", None), (61, "approval_required", "VP")],
    )
    def test_presubmit_by_age(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        age: int,
        flag_type: str | None,
        approver: str | None,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags = _clause(
            _presubmit(db_session, emp, _draft("meals", 20.0, TODAY - timedelta(days=age))),
            "EXP-01",
        )
        if flag_type is None:
            assert flags == []
            return
        (flag,) = flags
        assert flag.flag_type == flag_type and flag.severity == "advisory"
        assert flag.requires_approver == approver and flag.entry_ref == "Report"

    def test_the_earliest_line_sets_the_clock(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags = _presubmit(
            db_session,
            emp,
            _draft("meals", 20.0, TODAY - timedelta(days=40)),
            _draft("meals", 15.0, TODAY - timedelta(days=1)),
        )
        (flag,) = _clause(flags, "EXP-01")
        assert flag.observed["age_days"] == 40

    def test_trip_completion_anchors_a_trip_report(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Integration (security review, intended change): the clock starts at the
        EARLIER of the trusted trip end and the earliest expense.  A line dated after
        the trip ended (a late-posted charge) is due from the trip's end; a line dated
        before it is due from its own date (see the next tests)."""
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed(db_session, org_a)
        own = f.make_trip(
            db_session,
            traveler=emp,
            start_date=TODAY - timedelta(days=60),
            end_date=TODAY - timedelta(days=45),
            status=TripStatus.approved,
        )
        line = _draft("meals", 20.0, TODAY - timedelta(days=20))
        (flag,) = _clause(_presubmit(db_session, emp, line, trip_id=own.id), "EXP-01")
        assert flag.flag_type == "late_submission"
        assert flag.observed["basis"] == "trip completion" and flag.observed["age_days"] == 45
        # Someone else's trip is never the anchor (the lines' own dates are): 20 days.
        other = f.make_trip(
            db_session,
            traveler=mgr,
            start_date=TODAY - timedelta(days=60),
            end_date=TODAY - timedelta(days=45),
            status=TripStatus.approved,
        )
        assert _clause(_presubmit(db_session, emp, line, trip_id=other.id), "EXP-01") == []

    @pytest.mark.parametrize(
        "status",
        [TripStatus.draft, TripStatus.cancelled, TripStatus.pending_approval, TripStatus.approved],
    )
    def test_a_future_trip_never_suppresses_the_stale_flag(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        status: TripStatus,
    ) -> None:
        """Linking a stale report to an own trip that ends in the FUTURE (draft,
        cancelled, pending or even approved — dates are claimant-set on POST /trips)
        never moves the EXP-01 clock: the lines' own dates anchor it."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        future = f.make_trip(
            db_session,
            traveler=emp,
            start_date=TODAY + timedelta(days=200),
            end_date=TODAY + timedelta(days=300),
            status=status,
        )
        line = _draft("meals", 20.0, TODAY - timedelta(days=90))
        (flag,) = _clause(_presubmit(db_session, emp, line, trip_id=future.id), "EXP-01")
        assert flag.flag_type == "approval_required" and flag.requires_approver == "VP"
        assert flag.observed["basis"] == "the earliest expense"
        assert flag.observed["age_days"] == 90

    @pytest.mark.parametrize(
        "status",
        [TripStatus.draft, TripStatus.cancelled, TripStatus.rejected, TripStatus.pending_approval],
    )
    def test_an_unapproved_past_trip_is_not_an_anchor(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        status: TripStatus,
    ) -> None:
        """Only an approved / completed trip's end date is trusted as the completion."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        trip = f.make_trip(
            db_session,
            traveler=emp,
            start_date=TODAY - timedelta(days=95),
            end_date=TODAY - timedelta(days=5),
            status=status,
        )
        line = _draft("meals", 20.0, TODAY - timedelta(days=90))
        (flag,) = _clause(_presubmit(db_session, emp, line, trip_id=trip.id), "EXP-01")
        assert flag.requires_approver == "VP"
        assert flag.observed["basis"] == "the earliest expense"

    def test_lines_long_before_the_trip_anchor_on_their_own_date(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """An approved, finished trip never launders lines dated before it.

        Integration (security review, intended change): the completion is
        min(trip end, earliest expense), so every line dated before the trip's end —
        a 200-day-old meal, and now also a fare booked 50 days ahead (round 1 let an
        in-window pre-booked fare anchor on the trip end) — keeps its own date."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        trip = f.make_trip(
            db_session,
            traveler=emp,
            start_date=TODAY - timedelta(days=10),
            end_date=TODAY - timedelta(days=5),
            status=TripStatus.approved,
        )
        stale = _draft("meals", 20.0, TODAY - timedelta(days=200))
        (flag,) = _clause(_presubmit(db_session, emp, stale, trip_id=trip.id), "EXP-01")
        assert flag.requires_approver == "VP" and flag.observed["age_days"] == 200
        assert flag.observed["basis"] == "the earliest expense"
        fare = _draft("airfare", 300.0, TODAY - timedelta(days=50))
        (flag,) = _clause(_presubmit(db_session, emp, fare, trip_id=trip.id), "EXP-01")
        assert flag.flag_type == "late_submission" and flag.observed["age_days"] == 50
        assert flag.observed["basis"] == "the earliest expense"
        # Lines within the trip, ended 5 days ago: in time.
        meal = _draft("meals", 20.0, TODAY - timedelta(days=7))
        assert _clause(_presubmit(db_session, emp, meal, trip_id=trip.id), "EXP-01") == []

    def test_a_linked_future_trip_still_escalates_a_submitted_stale_report(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        for status in (TripStatus.draft, TripStatus.cancelled, TripStatus.approved):
            trip = f.make_trip(
                db_session,
                traveler=emp,
                start_date=TODAY + timedelta(days=200),
                end_date=TODAY + timedelta(days=300),
                status=status,
            )
            report = self._submitted(db_session, emp, TODAY - timedelta(days=90))
            report.trip_id = trip.id
            db_session.flush()
            flags, route = self._route(db_session, report)
            (stale,) = _clause(flags, "EXP-01")
            assert stale["requires_approver"] == "VP", status
            assert route == "escalate", (status, flags)

    def test_no_rule_no_flag(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a, process=False)
        flags = _presubmit(db_session, emp, _draft("meals", 20.0, TODAY - timedelta(days=90)))
        assert _clause(flags, "EXP-01") == []

    def _submitted(self, db: Session, emp: User, day: date) -> ExpenseReport:
        report = f.make_expense_report(
            db, owner=emp, title="Old lunch", status=ExpenseReportStatus.submitted
        )
        f.make_expense(
            db,
            report=report,
            category=ExpenseCategory.meals,
            amount="20.00",
            vendor="Corner Cafe",
            expense_date=day,
            match_source=ExpenseMatchSource.manual,
        )
        report.total_amount = Decimal("20.00")
        db.flush()
        return report

    def _route(self, db: Session, report: ExpenseReport) -> tuple[list[dict], str]:
        db.expire_all()
        fresh = db.get(ExpenseReport, report.id)
        assert fresh is not None
        owner = db.get(User, fresh.owner_id)
        assert owner is not None
        a = assess_risk(fresh, ApprovalSubjectType.expense_report, context=_ctx(owner), session=db)
        return list(a.flags_snapshot), decide_route(a)

    def test_a_stale_report_never_auto_approves(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags, route = self._route(
            db_session, self._submitted(db_session, emp, TODAY - timedelta(days=61))
        )
        (stale,) = _clause(flags, "EXP-01")
        assert stale["requires_approver"] == "VP" and route == "escalate"
        # Control: the same small report on time auto-approves; 31-60 days late is
        # advisory only (the clause names no approver for it).
        for age in (10, 45):
            _flags, ok = self._route(
                db_session, self._submitted(db_session, emp, TODAY - timedelta(days=age))
            )
            assert ok == "auto_approve", (age, _flags)

    def test_read_time_flag_on_an_untouched_draft(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        report = f.make_expense_report(db_session, owner=emp, title="Forgotten draft")
        f.make_expense(
            db_session,
            report=report,
            amount="20.00",
            expense_date=TODAY - timedelta(days=75),
            vendor="Corner Cafe",
        )
        assert report.pre_submit_flags is None
        body = employee_client.get(f"{API}/expense-reports/{report.id}").json()
        (flag,) = _clause(body["pre_submit_flags"]["flags"], "EXP-01")
        assert flag["flag_type"] == "approval_required" and flag["requires_approver"] == "VP"
        assert body["pre_submit_flags"]["advisory_count"] == 1
        db_session.expire_all()
        stored = db_session.get(ExpenseReport, report.id)
        assert stored is not None and stored.pre_submit_flags is None  # read only

    def test_a_submitted_report_keeps_the_flags_it_was_judged_on(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        """Read-time recompute is for DRAFTS only: a report submitted on time is shown
        to its reader with the stored snapshot, never a growing late / stale (EXP-01)
        or overdue-card (CARD-03) flag its submission was never judged on."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        snapshot = {
            "flags": [],
            "blocking_count": 0,
            "advisory_count": 0,
            "evaluated_at": "2026-08-01T00:00:00+00:00",
        }
        report = f.make_expense_report(
            db_session,
            owner=emp,
            title="Submitted on time",
            status=ExpenseReportStatus.submitted,
            pre_submit_flags=snapshot,
        )
        f.make_expense(
            db_session,
            report=report,
            amount="20.00",
            expense_date=TODAY - timedelta(days=90),
            vendor="Corner Cafe",
        )
        f.make_card_transaction(db_session, user=emp, txn_date=TODAY - timedelta(days=60))
        resp = employee_client.get(f"{API}/expense-reports/{report.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pre_submit_flags"] == snapshot
        assert _clause(body["pre_submit_flags"]["flags"], "EXP-01") == []
        assert _clause(body["pre_submit_flags"]["flags"], "CARD-03") == []
        # Control: the same lines on a DRAFT do get the read-time flags.
        report.status = ExpenseReportStatus.draft
        db_session.flush()
        body = employee_client.get(f"{API}/expense-reports/{report.id}").json()
        assert _clause(body["pre_submit_flags"]["flags"], "EXP-01")
        assert _clause(body["pre_submit_flags"]["flags"], "CARD-03")


# ---------------------------------------------------------------------------
# CARD-03
# ---------------------------------------------------------------------------


class TestCardReconciliation:
    def test_overdue_unreconciled_transactions_only(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed(db_session, org_a)
        line = _draft("meals", 20.0, TODAY - timedelta(days=2))
        f.make_card_transaction(db_session, user=emp, txn_date=TODAY - timedelta(days=30))
        assert _clause(_presubmit(db_session, emp, line), "CARD-03") == []
        f.make_card_transaction(
            db_session, user=emp, amount="55.10", txn_date=TODAY - timedelta(days=31)
        )
        # Reconciled (linked to an expense) and other people's transactions never count.
        report = f.make_expense_report(db_session, owner=emp)
        linked = f.make_expense(db_session, report=report, expense_date=TODAY)
        f.make_card_transaction(
            db_session, user=emp, txn_date=TODAY - timedelta(days=90), expense_id=linked.id
        )
        f.make_card_transaction(db_session, user=mgr, txn_date=TODAY - timedelta(days=90))
        (flag,) = _clause(_presubmit(db_session, emp, line), "CARD-03")
        assert flag.flag_type == "unreconciled_card" and flag.severity == "advisory"
        assert flag.observed["overdue_count"] == 1 and "55.10 USD" in flag.reason
        assert flag.requires_approver is None


# ---------------------------------------------------------------------------
# EXP-04 — missing-receipt affidavit
# ---------------------------------------------------------------------------


def _patch(client: TestClient, db: Session, entry: Expense, body: dict) -> Any:
    report = db.get(ExpenseReport, entry.report_id)
    assert report is not None
    db.refresh(report)
    return client.patch(
        f"{API}/expenses/{entry.id}", json=body, headers={"If-Match": str(report.version)}
    )


def _stored_flags(db: Session, entry: Expense) -> list[dict]:
    db.expire_all()
    report = db.get(ExpenseReport, entry.report_id)
    assert report is not None
    return [
        x
        for x in (report.pre_submit_flags or {}).get("flags", [])
        if x.get("entry_id") == str(entry.id) and x.get("flag_type") == "missing_receipt"
    ]


class TestAffidavit:
    def _line(self, db: Session, emp: User, amount: str, day: date = TODAY) -> Expense:
        report = f.make_expense_report(db, owner=emp, title=f"R {uuid.uuid4().hex[:5]}")
        return f.make_expense(
            db,
            report=report,
            category=ExpenseCategory.meals,
            amount=amount,
            vendor="Corner Cafe",
            expense_date=day,
        )

    def test_signing_is_audited_and_makes_the_line_submittable(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        entry = self._line(db_session, emp, "60.00")
        r = _patch(employee_client, db_session, entry, {"justification": "Receipt lost in a cab"})
        assert r.status_code == 200, r.text
        (flag,) = _stored_flags(db_session, entry)
        assert (
            flag["severity"] == "blocking"
            and "sign the missing-receipt affidavit" in flag["reason"]
        )

        r = _patch(employee_client, db_session, entry, {"receipt_affidavit": True})
        assert r.status_code == 200, r.text
        assert r.json()["receipt_affidavit_at"] is not None
        (flag,) = _stored_flags(db_session, entry)
        assert flag["severity"] == "advisory" and flag["source_clause_ref"] == "EXP-04"
        assert flag["observed"]["affidavit_accepted"] is True
        # One test transaction: the audit rows share a timestamp, so find the signing.
        audits = db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.entity_id == entry.id, AuditLog.action == "expense.updated"
            )
        ).all()
        (signed,) = [
            a
            for a in audits
            if (a.before_state or {}).get("receipt_affidavit_at") is None
            and (a.after_state or {}).get("receipt_affidavit_at") is not None
        ]
        assert signed.actor_id == emp.id and signed.organization_id == org_a.id

        # Withdrawn -> blocking again.
        r = _patch(employee_client, db_session, entry, {"receipt_affidavit": False})
        assert r.status_code == 200 and r.json()["receipt_affidavit_at"] is None
        (flag,) = _stored_flags(db_session, entry)
        assert flag["severity"] == "blocking"

    def test_only_the_owner_signs(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        manager_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        entry = self._line(db_session, emp, "60.00")
        r = _patch(manager_client, db_session, entry, {"receipt_affidavit": True})
        assert r.status_code == 403
        db_session.expire_all()
        unsigned = db_session.get(Expense, entry.id)
        assert unsigned is not None and unsigned.receipt_affidavit_at is None

    def test_over_the_ceiling_stays_blocking(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        """Integration (intended change): signing above the ceiling is refused (422) —
        the editor does not offer it either.  A signature already stored on such a line
        (legacy data) still never makes it submittable."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        entry = self._line(db_session, emp, "75.01")
        r = _patch(
            employee_client,
            db_session,
            entry,
            {"receipt_affidavit": True, "justification": "Lost it"},
        )
        assert r.status_code == 422, r.text
        legacy = self._line(db_session, emp, "75.01")
        legacy.receipt_affidavit_at = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
        db_session.flush()
        r = _patch(employee_client, db_session, legacy, {"justification": "Lost it"})
        assert r.status_code == 200, r.text
        (flag,) = _stored_flags(db_session, legacy)
        assert flag["severity"] == "blocking"
        assert flag["observed"]["affidavit_issue"] == "over_amount"
        ok = self._line(db_session, emp, "75.00")
        _patch(
            employee_client, db_session, ok, {"receipt_affidavit": True, "justification": "Lost it"}
        )
        (flag,) = _stored_flags(db_session, ok)
        assert flag["severity"] == "advisory"

    def test_an_attached_receipt_is_not_a_lost_one(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """A receipt that does not cover the claim is not replaced by an affidavit."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = self._line(db_session, emp, "60.00")
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            expense_id=line.id,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": "12.00", "currency": "USD", "date": TODAY.isoformat()},
            field_provenance={"amount": "deterministic", "currency": "deterministic"},
        )
        entry = _draft(
            "meals",
            60.0,
            TODAY,
            entry_id=str(line.id),
            primary_receipt_id=str(receipt.id),
            justification="Receipt lost",
            receipt_affidavit_at=datetime(2026, 11, 2, tzinfo=timezone.utc).isoformat(),
        )
        (flag,) = [
            x for x in _presubmit(db_session, emp, entry) if x.flag_type == "missing_receipt"
        ]
        assert flag.severity == "blocking" and flag.source_clause_ref == "RECEIPT-REQ"
        assert "affidavit_signed" not in flag.observed

    def test_fourth_affidavit_of_the_quarter_is_blocking(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed(db_session, org_a)
        base = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
        # Three earlier affidavits in Q4 2026, one in Q3 (other quarter) and one by
        # another employee: only the three count.
        for i in range(3):
            self._line(db_session, emp, "30.00", date(2026, 10, 5 + i)).receipt_affidavit_at = (
                base + timedelta(hours=i)
            )
        self._line(db_session, emp, "30.00", date(2026, 9, 30)).receipt_affidavit_at = base
        self._line(db_session, mgr, "30.00", date(2026, 10, 9)).receipt_affidavit_at = base
        fourth = self._line(db_session, emp, "30.00", date(2026, 11, 2))
        fourth.receipt_affidavit_at = base + timedelta(days=1)
        fourth.justification = "Receipt lost"
        db_session.flush()
        entry = _draft(
            "meals",
            30.0,
            date(2026, 11, 2),
            entry_id=str(fourth.id),
            justification="Receipt lost",
            receipt_affidavit_at=_iso(fourth.receipt_affidavit_at),
        )
        (flag,) = [
            x for x in _presubmit(db_session, emp, entry) if x.flag_type == "missing_receipt"
        ]
        assert flag.severity == "blocking" and flag.observed["affidavit_issue"] == "over_count"
        assert flag.observed["affidavits_before_in_quarter"] == 3
        # The third of the quarter keeps its place.
        third = db_session.scalars(
            sa.select(Expense).where(Expense.expense_date == date(2026, 10, 7))
        ).one()
        e3 = _draft(
            "meals",
            30.0,
            date(2026, 10, 7),
            entry_id=str(third.id),
            justification="Receipt lost",
            receipt_affidavit_at=_iso(third.receipt_affidavit_at),
        )
        (flag3,) = [x for x in _presubmit(db_session, emp, e3) if x.flag_type == "missing_receipt"]
        assert flag3.severity == "advisory" and "affidavit 3 of 3" in flag3.reason


# ---------------------------------------------------------------------------
# Trips — PTA-02 / AIR-06 / AIR-04 / PTA-05 at approval
# ---------------------------------------------------------------------------


def _planner_trip(
    db: Session,
    emp: User,
    *,
    depart: date,
    fare: str = "300.00",
    lowest: str | None = "300.00",
    cabin: str = "economy",
    hours: float = 5.5,
    origin: str = "SFO",
    destination: str = "JFK",
    status: TripStatus = TripStatus.pending_approval,
    estimated: str | None = None,
    extra_ctx: dict | None = None,
) -> Trip:
    trip = f.make_trip(
        db,
        traveler=emp,
        origin=origin,
        primary_destination=destination,
        start_date=depart,
        end_date=depart,
        status=status,
        estimated_cost=Decimal(estimated or fare),
    )
    option = f"flt-{uuid.uuid4().hex[:8]}"
    f.make_trip_segment(
        db,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="United Airlines",
        amount=Decimal(fare),
        currency="USD",
        in_policy=True,
        details={"option_id": option, "cabin_class": cabin, "duration_hours": hours},
    )
    trip.recommendation_context = {
        "agent_task": "trip_planning",
        "selected_option_ids": [option],
        "selected_options": [{"option_id": option, "lowest_fare_usd": lowest}],
        **(extra_ctx or {}),
    }
    db.flush()
    return trip


def _trip_route(db: Session, trip: Trip) -> tuple[list[dict], str]:
    db.expire_all()
    fresh = db.get(Trip, trip.id)
    assert fresh is not None
    traveler = db.get(User, fresh.traveler_id)
    assert traveler is not None
    a = assess_risk(fresh, ApprovalSubjectType.trip, context=_ctx(traveler), session=db)
    return list(a.flags_snapshot), decide_route(a)


class TestTripApproval:
    def test_pta02_short_notice_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags, route = _trip_route(
            db_session, _planner_trip(db_session, emp, depart=TODAY + timedelta(days=13))
        )
        (flag,) = _clause(flags, "PTA-02")
        assert flag["flag_type"] == "approval_required" and flag["requires_approver"] == "manager"
        assert "Booked 13 days ahead" in flag["reason"] and route == "escalate"
        # Control: 14 days ahead, everything else equal -> auto-approved.
        flags, route = _trip_route(
            db_session, _planner_trip(db_session, emp, depart=TODAY + timedelta(days=14))
        )
        assert _clause(flags, "PTA-02") == [] and route == "auto_approve", flags

    def test_pta02_international_needs_21_days(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags, _ = _trip_route(
            db_session,
            _planner_trip(
                db_session,
                emp,
                depart=TODAY + timedelta(days=20),
                origin="JFK",
                destination="LHR",
                hours=7.0,
            ),
        )
        (flag,) = _clause(flags, "PTA-02")
        assert "at least 21 days" in flag["reason"]

    def test_air06_above_the_search_tolerance(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        depart = TODAY + timedelta(days=30)
        flags, route = _trip_route(
            db_session, _planner_trip(db_session, emp, depart=depart, fare="450.01", lowest="300")
        )
        (flag,) = _clause(flags, "AIR-06")
        assert flag["flag_type"] == "policy_violation" and route == "escalate"
        # Control (another day — an overlapping trip would be a possible split trip).
        flags, route = _trip_route(
            db_session,
            _planner_trip(
                db_session, emp, depart=depart + timedelta(days=3), fare="450.00", lowest="300"
            ),
        )
        assert _clause(flags, "AIR-06") == [] and route == "auto_approve", flags

    def test_air06_typed_in_flight_is_could_not_verify(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        trip = _planner_trip(db_session, emp, depart=TODAY + timedelta(days=30))
        trip.recommendation_context = None  # POST /trips: no planner context
        db_session.flush()
        flags, _ = _trip_route(db_session, trip)
        (flag,) = _clause(flags, "AIR-06")
        assert flag["flag_type"] == "not_evaluated" and flag["requires_approver"] is None

    def test_air06_baseline_needs_the_planner_selection(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """``details.option_id`` is client-writable: a segment naming an option the planner
        did not select for THIS trip gets no search baseline (could not verify)."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        trip = _planner_trip(db_session, emp, depart=TODAY + timedelta(days=30), fare="900")
        ctx = dict(trip.recommendation_context or {})
        ctx["selected_option_ids"] = ["someone-elses-option"]
        trip.recommendation_context = ctx
        db_session.flush()
        flags, _ = _trip_route(db_session, trip)
        (flag,) = _clause(flags, "AIR-06")
        assert flag["flag_type"] == "not_evaluated"

    def test_air04_business_over_six_hours(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        flags, route = _trip_route(
            db_session,
            _planner_trip(
                db_session,
                emp,
                depart=TODAY + timedelta(days=40),
                cabin="business",
                hours=7.0,
                fare="1200",
                lowest="1200",
                origin="JFK",
                destination="LHR",
            ),
        )
        (flag,) = _clause(flags, "AIR-04")
        assert flag["requires_approver"] == "SVP" and route == "escalate"

    @pytest.mark.parametrize(("estimate", "reapproval"), [("1100.00", False), ("1100.10", True)])
    def test_pta05_change_of_an_approved_trip(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        estimate: str,
        reapproval: bool,
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed(db_session, org_a)
        depart = TODAY + timedelta(days=30)
        approved = _planner_trip(
            db_session, emp, depart=depart, status=TripStatus.approved, estimated="1000.00"
        )
        change = _planner_trip(
            db_session,
            emp,
            depart=depart,
            estimated=estimate,
            extra_ctx={"changes_trip_id": str(approved.id)},
        )
        flags, route = _trip_route(db_session, change)
        found = _clause(flags, "PTA-05")
        if not reapproval:
            assert found == []
        else:
            (flag,) = found
            assert flag["requires_approver"] == "manager" and "10.01% more" in flag["reason"]
            assert flag["related_subject_id"] == str(approved.id)
        assert route == "escalate"  # > $500 either way
        # A link to someone else's trip is ignored (never a baseline).
        foreign = _planner_trip(
            db_session, mgr, depart=depart, status=TripStatus.approved, estimated="10.00"
        )
        sneaky = _planner_trip(
            db_session,
            emp,
            depart=depart,
            estimated="1100.10",
            extra_ctx={"changes_trip_id": str(foreign.id)},
        )
        flags, _ = _trip_route(db_session, sneaky)
        assert _clause(flags, "PTA-05") == []


# ---------------------------------------------------------------------------
# Planner — notices at selection time, PTA-05 link
# ---------------------------------------------------------------------------


class TestPlanner:
    def test_short_notice_flight_options_carry_pta02(
        self,
        planner_app: Any,  # noqa: F811
        rbac_catalog: None,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        today = date.today()
        monkeypatch.setattr(process_checks, "today_utc", lambda: today)
        emp = users_a["employee"]
        _seed(db_session, org_a)
        p = _Planner(_client(planner_app, emp))
        depart = today + timedelta(days=5)
        out = p.send(
            "Plan",
            departure_date=depart.isoformat(),
            return_date=(depart + timedelta(days=2)).isoformat(),
        )
        flights = [o for o in out["shortlist"] if o["segment_type"] == "flight"]
        assert flights
        for o in flights:
            pta = [n for n in o["policy_notices"] if n["clause_ref"] == "PTA-02"]
            assert pta and "Booked 5 days ahead" in pta[0]["message"]
            assert "at least 14 days" in pta[0]["message"]
            assert o["lowest_fare_usd"] is not None
        hotels = [o for o in out["shortlist"] if o["segment_type"] == "hotel"]
        assert all(o["policy_notices"] == [] for o in hotels)
        chosen = p.send(
            "Plan my trip",
            departure_date=depart.isoformat(),
            return_date=(depart + timedelta(days=2)).isoformat(),
            selected_option_ids=[flights[0]["option_id"]],
        )
        assert "Policy notice — PTA-02: Booked 5 days ahead" in chosen["trip_message"]
        trip = db_session.get(Trip, uuid.UUID(chosen["trip_id"]))
        assert trip is not None
        snap = (trip.recommendation_context or {})["selected_options"][0]
        assert snap["lowest_fare_usd"] == flights[0]["lowest_fare_usd"]

    def test_changing_a_submitted_trip_links_it_and_names_reapproval(
        self,
        planner_app: Any,  # noqa: F811
        rbac_catalog: None,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(process_checks, "today_utc", date.today)
        emp = users_a["employee"]
        _seed(db_session, org_a)
        client = _client(planner_app, emp)
        p = _Planner(client)
        flights, _hotels = p.options()
        economy = min(
            (o for o in flights if o["cabin_class"] == "economy"), key=lambda o: o["amount_usd"]
        )
        business = next(o for o in flights if o["cabin_class"] == "business")
        first = p.select(economy["option_id"])
        r = client.patch(f"{API}/trips/{first['trip_id']}", json={"status": "pending_approval"})
        assert r.status_code == 200, r.text

        second = p.select(business["option_id"])
        assert second["trip_id"] != first["trip_id"]
        change = second["trip_change"]
        assert change["changes_trip_id"] == first["trip_id"]
        assert change["reapproval_required"] is True
        assert (
            "PTA-05" in second["trip_message"] and "must be re-approved" in second["trip_message"]
        )
        new_trip = db_session.get(Trip, uuid.UUID(second["trip_id"]))
        assert new_trip is not None
        assert (new_trip.recommendation_context or {})["changes_trip_id"] == first["trip_id"]
        created = db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.entity_id == new_trip.id, AuditLog.action == "trip.created"
            )
        ).one()
        assert (created.after_state or {})["changes_trip_id"] == first["trip_id"]
        assert (created.after_state or {})["reapproval_required"] is True
        assert created.actor_id == emp.id

        # Submitting the change: never auto-approved, PTA-05 cites the increase.
        r2 = client.patch(f"{API}/trips/{second['trip_id']}", json={"status": "pending_approval"})
        assert r2.status_code == 200 and r2.json()["status"] == "pending_approval", r2.text
        from database.models.approval import Approval

        approval = db_session.scalar(
            sa.select(Approval).where(Approval.subject_id == uuid.UUID(second["trip_id"]))
        )
        assert approval is not None and approval.current_status == ApprovalStatus.pending
        assert _clause((approval.risk_summary or {})["flags"], "PTA-05")
