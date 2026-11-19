"""Integration round 2 — open items on the merged policy tracks, on Postgres.

EXP-04 (blocker + affidavit gaming)
  * only an affidavit standing in for a REQUIRED receipt (RECEIPT-REQ threshold /
    category, converted to USD) uses up one of the quarter's 3 slots, and signing is
    refused where the entry editor does not offer it;
  * a signature is withdrawn when the claim it vouches for changes (date, amount,
    currency, vendor, description) or a receipt is linked — re-signing stamps a fresh
    time; a junk receipt never frees a slot, a VALIDATED receipt on a LOCKED line does;
  * the approval-time evaluation (at submission) re-counts the quarter — a stale draft
    snapshot is never trusted.
PTA-02  a typed far-future segment date cannot lengthen the lead time.
Hardening
  * POST /expense-reports/{id}/mileage needs If-Match or an Idempotency-Key;
  * ``attendee_count`` is a strict integer;
  * migrations 0017-0019 on a SEEDED database insert / rewrite nothing and leave
    admin-edited rules alone; 0018's downgrade removes the seeded process rules but
    never an admin-authored one.
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
from backend.tests.test_pg2_process_rules_db import (
    TODAY,
    _clause,
    _ctx,
    _draft,
    _iso,
    _patch,
    _presubmit,
    _seed,
    _stored_flags,
    _trip_route,
)
from backend.tests.test_pg2_spend_mileage_db import TRIP, _report
from backend.tests.test_pg2_spend_mileage_db import policy as policy  # noqa: F401
from database.models.approval import ApprovalSubjectType
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
from database.models.receipt import OcrStatus
from database.models.trip import TripStatus
from database.models.trip_segment import SegmentType
from database.seeds.policy_content import ENTRY_RULES, PROCESS_RULES, RULES

pytestmark = pytest.mark.db

API = "/api/v1"
_VERSIONS = Path(__file__).resolve().parents[2] / "database" / "migrations" / "versions"
SIGNED = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


def _line(
    db: Session,
    emp: User,
    amount: str,
    day: date,
    *,
    currency: str = "USD",
    status: ExpenseReportStatus = ExpenseReportStatus.draft,
    category: ExpenseCategory = ExpenseCategory.meals,
) -> Expense:
    report = f.make_expense_report(db, owner=emp, title=f"R {uuid.uuid4().hex[:5]}", status=status)
    return f.make_expense(
        db,
        report=report,
        category=category,
        amount=amount,
        currency=currency,
        vendor="Corner Cafe",
        expense_date=day,
    )


def _receipt(db: Session, emp: User, line: Expense, *, amount: str | None = None) -> Any:
    return f.make_receipt(
        db,
        uploaded_by=emp,
        expense_id=line.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={
            "amount": amount if amount is not None else str(line.amount),
            "currency": "USD",
            "date": str(line.expense_date),
        },
        field_provenance={"amount": "deterministic", "currency": "deterministic"},
    )


def _signed(db: Session, line: Expense, at: datetime) -> None:
    line.receipt_affidavit_at = at
    db.flush()


def _affidavit(db: Session, emp: User, line: Expense) -> Any:
    entry = _draft(
        str(line.category.value),
        float(str(line.amount)),
        line.expense_date,  # type: ignore[arg-type]
        entry_id=str(line.id),
        justification="Receipt lost",
        receipt_affidavit_at=_iso(line.receipt_affidavit_at),
    )
    (flag,) = [x for x in _presubmit(db, emp, entry) if x.flag_type == "missing_receipt"]
    return flag


def _fourth(db: Session, emp: User) -> Expense:
    line = _line(db, emp, "30.00", date(2026, 11, 2))
    _signed(db, line, SIGNED + timedelta(days=30))
    return line


# ---------------------------------------------------------------------------
# EXP-04 blocker — only affidavits standing in for a REQUIRED receipt count
# ---------------------------------------------------------------------------


class TestAffidavitsForRequiredReceiptsOnly:
    def test_affidavits_on_lines_that_need_no_receipt_use_no_slot(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Three $10 coffees signed earlier (no receipt needed under $25) do not make a
        genuine $30 affidavit the '4th of the quarter'."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        for i in range(3):
            small = _line(db_session, emp, "10.00", date(2026, 10, 5 + i))
            _signed(db_session, small, SIGNED + timedelta(hours=i))
        flag = _affidavit(db_session, emp, _fourth(db_session, emp))
        assert flag.severity == "advisory", flag.reason
        assert flag.observed["affidavits_before_in_quarter"] == 0
        assert "affidavit 1 of 3" in flag.reason

    def test_the_count_converts_to_usd(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """EUR 22 (≈ $23.87, no receipt needed) and EUR 70 (≈ $75.95, above the
        ceiling) use no slot; USD 30 and EUR 30 (≈ $32.55) do."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        for i, (amount, ccy) in enumerate(
            [("22.00", "EUR"), ("70.00", "EUR"), ("30.00", "USD"), ("30.00", "EUR")]
        ):
            line = _line(db_session, emp, amount, date(2026, 10, 5 + i), currency=ccy)
            _signed(db_session, line, SIGNED + timedelta(hours=i))
        flag = _affidavit(db_session, emp, _fourth(db_session, emp))
        assert flag.severity == "advisory", flag.reason
        assert flag.observed["affidavits_before_in_quarter"] == 2

    def test_signing_is_refused_where_the_editor_does_not_offer_it(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        coffee = _line(db_session, emp, "10.00", TODAY)
        r = _patch(employee_client, db_session, coffee, {"receipt_affidavit": True})
        assert r.status_code == 422, r.text
        receipted = _line(db_session, emp, "30.00", TODAY)
        _receipt(db_session, emp, receipted, amount="5.00")
        r = _patch(employee_client, db_session, receipted, {"receipt_affidavit": True})
        assert r.status_code == 422, r.text
        lost = _line(db_session, emp, "30.00", TODAY)
        r = _patch(employee_client, db_session, lost, {"receipt_affidavit": True})
        assert r.status_code == 200 and r.json()["receipt_affidavit_at"], r.text
        # Withdrawing is always allowed.
        _signed(db_session, coffee, SIGNED)
        r = _patch(employee_client, db_session, coffee, {"receipt_affidavit": False})
        assert r.status_code == 200 and r.json()["receipt_affidavit_at"] is None


# ---------------------------------------------------------------------------
# EXP-04 gaming — a signature vouches for one claim
# ---------------------------------------------------------------------------


class TestSignatureFollowsTheClaim:
    @pytest.mark.parametrize(
        "change",
        [
            {"amount": "31.00"},
            {"expense_date": (TODAY - timedelta(days=1)).isoformat()},
            {"currency": "EUR"},
            {"vendor": "Other Cafe"},
            {"description": "Dinner"},
        ],
    )
    def test_changing_the_claim_withdraws_the_signature(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
        change: dict,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = _line(db_session, emp, "30.00", TODAY)
        r = _patch(
            employee_client,
            db_session,
            line,
            {"receipt_affidavit": True, "justification": "Receipt lost"},
        )
        assert r.status_code == 200, r.text
        first = r.json()["receipt_affidavit_at"]
        r = _patch(employee_client, db_session, line, change)
        assert r.status_code == 200, r.text
        assert r.json()["receipt_affidavit_at"] is None
        (flag,) = _stored_flags(db_session, line)
        assert flag["severity"] == "blocking"
        # Re-signing is a NEW signature: a fresh time, after the first.
        r = _patch(employee_client, db_session, line, {"receipt_affidavit": True})
        assert r.status_code == 200, r.text
        assert r.json()["receipt_affidavit_at"] > first

    def test_a_justification_edit_keeps_the_signature(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = _line(db_session, emp, "30.00", TODAY)
        first = _patch(employee_client, db_session, line, {"receipt_affidavit": True}).json()[
            "receipt_affidavit_at"
        ]
        r = _patch(employee_client, db_session, line, {"justification": "Lost in a taxi"})
        assert r.json()["receipt_affidavit_at"] == first

    def test_signing_with_a_change_in_the_same_edit_is_a_fresh_signature(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = _line(db_session, emp, "30.00", TODAY)
        first = _patch(employee_client, db_session, line, {"receipt_affidavit": True}).json()[
            "receipt_affidavit_at"
        ]
        r = _patch(
            employee_client, db_session, line, {"amount": "32.00", "receipt_affidavit": True}
        )
        assert r.status_code == 200 and r.json()["receipt_affidavit_at"] > first

    def test_linking_a_receipt_withdraws_the_signature(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = _line(db_session, emp, "30.00", TODAY)
        assert _patch(employee_client, db_session, line, {"receipt_affidavit": True}).json()[
            "receipt_affidavit_at"
        ]
        junk = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": "3.00", "currency": "USD", "date": TODAY.isoformat()},
            field_provenance={"amount": "deterministic"},
        )
        report = db_session.get(ExpenseReport, line.report_id)
        assert report is not None
        db_session.refresh(report)
        r = employee_client.post(
            f"{API}/expenses/{line.id}/receipts",
            json={"receipt_id": str(junk.id)},
            headers={"If-Match": str(report.version)},
        )
        assert r.status_code in (200, 201), r.text
        db_session.expire_all()
        stored = db_session.get(Expense, line.id)
        assert stored is not None and stored.receipt_affidavit_at is None


class TestOnlyAValidatedReceiptOnALockedLineFreesASlot:
    def _three_used(self, db: Session, emp: User) -> list[Expense]:
        """Three affidavits used on a SUBMITTED report."""
        lines = []
        for i in range(3):
            line = _line(
                db, emp, "30.00", date(2026, 10, 5 + i), status=ExpenseReportStatus.submitted
            )
            _signed(db, line, SIGNED + timedelta(hours=i))
            lines.append(line)
        return lines

    def test_a_junk_receipt_frees_nothing(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        lines = self._three_used(db_session, emp)
        _receipt(db_session, emp, lines[0], amount="3.00")  # does not cover the claim
        flag = _affidavit(db_session, emp, _fourth(db_session, emp))
        assert flag.severity == "blocking" and flag.observed["affidavit_issue"] == "over_count"

    def test_a_validated_receipt_on_a_locked_line_frees_its_slot(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        lines = self._three_used(db_session, emp)
        _receipt(db_session, emp, lines[0])  # machine-read, covers it, same date
        flag = _affidavit(db_session, emp, _fourth(db_session, emp))
        assert flag.severity == "advisory", flag.reason
        assert flag.observed["affidavits_before_in_quarter"] == 2

    def test_a_receipt_on_a_draft_line_frees_nothing(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """A draft line keeping both a signature and a receipt (legacy data — linking
        a receipt now withdraws the signature) still uses its slot."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        for i in range(3):
            line = _line(db_session, emp, "30.00", date(2026, 10, 5 + i))
            _receipt(db_session, emp, line)
            _signed(db_session, line, SIGNED + timedelta(hours=i))
        flag = _affidavit(db_session, emp, _fourth(db_session, emp))
        assert flag.severity == "blocking" and flag.observed["affidavit_issue"] == "over_count"


class TestQuarterRecheckedAtApprovalTime:
    def test_a_stale_accepted_snapshot_is_recounted_at_submission(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """The draft snapshot said 'affidavit 1 of 3'; by submission three earlier
        affidavits are in use.  The approval agent's fresh evaluation counts again:
        the 4th is blocking (EXP-04) and the report escalates."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        self_lines = TestOnlyAValidatedReceiptOnALockedLineFreesASlot()._three_used(db_session, emp)
        assert len(self_lines) == 3
        fourth = _fourth(db_session, emp)
        fourth.justification = "Receipt lost"
        report = db_session.get(ExpenseReport, fourth.report_id)
        assert report is not None
        report.pre_submit_flags = {
            "flags": [
                {
                    "entry_id": str(fourth.id),
                    "flag_type": "missing_receipt",
                    "severity": "advisory",
                    "source_clause_ref": "EXP-04",
                    "reason": "Missing-receipt affidavit signed (affidavit 1 of 3).",
                    "observed": {"affidavit_accepted": True},
                }
            ],
            "blocking_count": 0,
            "advisory_count": 1,
        }
        report.status = ExpenseReportStatus.submitted
        db_session.flush()
        db_session.expire_all()
        fresh = db_session.get(ExpenseReport, report.id)
        assert fresh is not None
        a = assess_risk(
            fresh, ApprovalSubjectType.expense_report, context=_ctx(emp), session=db_session
        )
        exp04 = [
            fl
            for fl in a.flags_snapshot
            if fl.get("source") == "approval_time_evaluation"
            and fl.get("source_clause_ref") == "EXP-04"
        ]
        assert exp04 and exp04[0]["severity"] == "blocking", a.flags_snapshot
        assert exp04[0]["observed"]["affidavit_issue"] == "over_count"
        assert decide_route(a) == "escalate"


# ---------------------------------------------------------------------------
# PTA-02 — a typed segment date
# ---------------------------------------------------------------------------


def test_pta02_a_typed_far_future_segment_cannot_lengthen_the_lead_time(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    _seed(db_session, org_a)
    trip = f.make_trip(
        db_session,
        traveler=emp,
        origin="SFO",
        primary_destination="JFK",
        start_date=TODAY + timedelta(days=5),
        end_date=TODAY + timedelta(days=7),
        status=TripStatus.pending_approval,
        estimated_cost=Decimal("300.00"),
    )
    far = TODAY + timedelta(days=60)
    f.make_trip_segment(
        db_session,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="United Airlines",
        amount=Decimal("300.00"),
        currency="USD",
        in_policy=True,
        start_datetime=datetime(far.year, far.month, far.day, 9, tzinfo=timezone.utc),
        details={"option_id": "typed-opt", "cabin_class": "economy", "duration_hours": 5.5},
    )
    flags, route = _trip_route(db_session, trip)
    (flag,) = _clause(flags, "PTA-02")
    assert "Booked 5 days ahead" in flag["reason"], flag
    assert route == "escalate"


# ---------------------------------------------------------------------------
# Hardening — mileage double submit, strict attendee count
# ---------------------------------------------------------------------------


def _mileage(client: TestClient, report: ExpenseReport, headers: dict) -> Any:
    return client.post(f"{API}/expense-reports/{report.id}/mileage", json=TRIP, headers=headers)


def _mileage_count(db: Session, report: ExpenseReport) -> int:
    return int(
        db.scalar(
            sa.select(sa.func.count(Expense.id)).where(
                Expense.report_id == report.id, Expense.category == ExpenseCategory.mileage
            )
        )
        or 0
    )


def test_mileage_needs_if_match_or_an_idempotency_key(
    policy: Policy,  # noqa: F811
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    report = _report(db_session, users_a["employee"])
    r = _mileage(employee_client, report, {})
    assert r.status_code == 428, r.text
    assert _mileage_count(db_session, report) == 0
    # The same If-Match twice: the second is stale.
    db_session.refresh(report)
    version = {"If-Match": str(report.version)}
    assert _mileage(employee_client, report, version).status_code == 201
    assert _mileage(employee_client, report, version).status_code == 409
    assert _mileage_count(db_session, report) == 1
    # The same Idempotency-Key twice: one entry, the second call replays it.
    key = {"Idempotency-Key": f"k-{uuid.uuid4()}"}
    first = _mileage(employee_client, report, key)
    again = _mileage(employee_client, report, key)
    assert first.status_code == 201 and again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert _mileage_count(db_session, report) == 2


@pytest.mark.parametrize("value", [True, "3", 3.0, "two"])
def test_attendee_count_is_a_strict_integer(
    policy: Policy,  # noqa: F811
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    value: Any,
) -> None:
    emp = users_a["employee"]
    meal = _line(db_session, emp, "80.00", TODAY)
    r = employee_client.patch(f"{API}/expenses/{meal.id}", json={"attendee_count": value})
    assert r.status_code == 422, r.text
    ok = employee_client.patch(f"{API}/expenses/{meal.id}", json={"attendee_count": 3})
    assert ok.status_code == 200 and ok.json()["attendee_count"] == 3


# ---------------------------------------------------------------------------
# Migrations 0017-0019 on a SEEDED database; 0018 downgrade
# ---------------------------------------------------------------------------


def _mig(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"integ_{name}", _VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seeded_policy(db: Session, org: Organization, admin: User) -> Policy:
    """What ``seed_all`` writes on a fresh database: every rule, created by the admin."""
    # The stub policy name: 0019 targets exactly that policy.
    name = _mig("0019_spend_caps_mileage").STUB_POLICY_NAME
    pol = f.make_policy(db, org=org, is_active=True, name=name)
    for r in (*RULES, *ENTRY_RULES, *PROCESS_RULES):
        f.make_policy_rule(
            db,
            policy=pol,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=json.loads(json.dumps(r["definition"])),
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
            created_by=admin.id,
        )
    return pol


def _rules(db: Session, pol: Policy) -> list[PolicyRule]:
    db.expire_all()
    return list(db.scalars(sa.select(PolicyRule).where(PolicyRule.policy_id == pol.id)).all())


class TestMigrationsOnASeededDatabase:
    def test_upgrade_after_a_fresh_seed_duplicates_and_rewrites_nothing(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        pol = _seeded_policy(db_session, org_a, users_a["finance"])
        before = {
            (r.source_clause_ref, json.dumps(r.definition, sort_keys=True))
            for r in _rules(db_session, pol)
        }
        conn = db_session.connection()
        _mig("0017_entry_level_policy_rules").apply_entry_level_rules(conn, forward=True)
        _mig("0018_process_timing_rules").apply_process_rules(conn, forward=True)
        assert _mig("0019_spend_caps_mileage").apply_spend_rules(conn) == 0
        rows = _rules(db_session, pol)
        refs = [r.source_clause_ref for r in rows]
        assert len(refs) == len(set(refs)), refs
        assert len(rows) == len(RULES) + len(ENTRY_RULES) + len(PROCESS_RULES)
        after = {(r.source_clause_ref, json.dumps(r.definition, sort_keys=True)) for r in rows}
        assert after == before
        # No migration audit row on these rules: nothing was touched.
        touched = db_session.scalars(
            sa.select(AuditLog).where(AuditLog.entity_id.in_([r.id for r in rows]))
        ).all()
        assert touched == []

    def test_admin_edited_rules_survive_the_upgrade(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        pol = _seeded_policy(db_session, org_a, users_a["finance"])
        by_ref = {r.source_clause_ref: r for r in _rules(db_session, pol)}
        by_ref["RECEIPT-REQ"].definition = {**by_ref["RECEIPT-REQ"].definition, "threshold_usd": 40}
        by_ref["MEAL-05"].definition = {**by_ref["MEAL-05"].definition, "min_attendees": 3}
        by_ref["GRND-01"].status = PolicyRuleStatus.archived
        by_ref["EXP-01"].status = PolicyRuleStatus.archived
        db_session.flush()
        conn = db_session.connection()
        _mig("0017_entry_level_policy_rules").apply_entry_level_rules(conn, forward=True)
        _mig("0018_process_timing_rules").apply_process_rules(conn, forward=True)
        _mig("0019_spend_caps_mileage").apply_spend_rules(conn)
        after = {r.source_clause_ref: r for r in _rules(db_session, pol)}
        assert len(after) == len(by_ref)
        assert after["RECEIPT-REQ"].definition["threshold_usd"] == 40
        assert after["MEAL-05"].definition["min_attendees"] == 3
        assert after["GRND-01"].status == PolicyRuleStatus.archived
        assert after["EXP-01"].status == PolicyRuleStatus.archived

    def test_0018_downgrade_removes_seeded_process_rules_not_admin_ones(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        admin = users_a["finance"]
        pol = _seeded_policy(db_session, org_a, admin)
        # An admin-authored rule that happens to equal the literal (it has a history
        # with an actor) and an admin-edited seeded rule.
        authored_policy = f.make_policy(db_session, org=org_a, name="Admin policy")
        lit = next(r for r in PROCESS_RULES if r["clause_ref"] == "PTA-02")
        authored = f.make_policy_rule(
            db_session,
            policy=authored_policy,
            rule_type=PolicyRuleType(lit["rule_type"]),
            applies_to=PolicyAppliesTo(lit["applies_to"]),
            definition=json.loads(json.dumps(lit["definition"])),
            source_clause_ref="PTA-02",
            created_by=admin.id,
        )
        db_session.add(
            AuditLog(
                organization_id=org_a.id,
                actor_id=admin.id,
                action="policy_rule.created",
                entity_type="policy_rule",
                entity_id=authored.id,
            )
        )
        edited = next(r for r in _rules(db_session, pol) if r.source_clause_ref == "AIR-06")
        edited.definition = {**edited.definition, "domestic_tolerance_usd": 200}
        db_session.flush()

        _mig("0018_process_timing_rules").apply_process_rules(
            db_session.connection(), forward=False
        )
        refs = {r.source_clause_ref for r in _rules(db_session, pol)}
        assert not refs & {"EXP-01", "PTA-02", "PTA-05", "CARD-03"}, refs
        assert "AIR-06" in refs  # admin-edited: kept
        assert db_session.get(PolicyRule, authored.id) is not None  # admin-authored: kept
