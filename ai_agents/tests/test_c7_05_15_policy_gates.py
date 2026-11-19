"""C7-05 / C7-15 regression tests — approval-time policy gate + pre-submit segment resolution.

Hermetic: no DB, no LLM.  ``PolicyEvalTool._load_active_rules`` is patched to
return the REAL seeded rule definitions (``database.seeds.policy_content.RULES``)
and the SQLAlchemy session is a tiny in-memory fake, so the full chain
    Expense rows → DraftEntry → run_presubmit(strict) → PolicyEvalTool → flags
    → RiskAssessment → decide_route
runs exactly as in production.

C7-05: reports created via the API/manual path carry ``pre_submit_flags=None``;
the approval agent used to read ONLY those stored flags and auto-approved
out-of-policy reports under $500 (SKYBRIDGE AIR 450 airfare, 389/night NYC hotel).

C7-15: ``run_presubmit`` hard-coded ``segment=None`` so assembly read supplier /
nights / city from a missing segment → false AIR-PREF "Supplier ''" and a false
HOTEL-CAP (city None → lowest cap, then domestic 180 / now Tier 3 160) on a 4-night
239/night NYC Marriott stay.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from ai_agents.agents.approval_agent.evaluate import RiskAssessment, assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import run_presubmit
from ai_agents.context import AgentContext
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
from database.seeds.policy_content import RULES

ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
EMPLOYEE = uuid.uuid4()
OTHER_EMPLOYEE = uuid.uuid4()
MANAGER = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _seed_rules() -> list[PolicyRule]:
    return [
        PolicyRule(
            id=uuid.uuid4(),
            policy_id=uuid.uuid4(),
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
        for r in RULES
    ]


@pytest.fixture(autouse=True)
def _seeded_rules():
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=_seed_rules()):
        yield


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Minimal Session stand-in: get() by (model, id); scalars()/execute() for the
    approval agent's Expense / Receipt / CardTransaction lookups."""

    def __init__(self) -> None:
        self.objects: dict[tuple[type, uuid.UUID], Any] = {}
        self.expenses: list[Expense] = []

    def add_obj(self, obj: Any) -> Any:
        self.objects[(type(obj), obj.id)] = obj
        if isinstance(obj, Expense):
            self.expenses.append(obj)
        return obj

    def get(self, model: type, ident: Any) -> Any:
        return self.objects.get((model, ident))

    def begin_nested(self) -> contextlib.AbstractContextManager[None]:
        """SAVEPOINT stand-in for the duplicate-claim lookup (C7R)."""
        return contextlib.nullcontext()

    def scalars(self, stmt: Any) -> _Rows:
        sql = str(stmt)
        if "FROM expenses" in sql:
            params = stmt.compile().params
            report_ids = {v for k, v in params.items() if k.startswith("report_id")}
            return _Rows([e for e in self.expenses if e.report_id in report_ids])
        if "FROM receipts" in sql and "?|" in sql:
            # Receipts SHARED with these entries (itemized siblings, UAT2
            # hardening): none in this fake; covered by the DB tests.
            return _Rows([])
        raise AssertionError(f"unexpected scalars(): {sql}")

    def scalar(self, stmt: Any) -> Any:
        """Answer presubmit's 'has this trip a HUMAN-approved approval row' probe."""
        sql = str(stmt)
        if "FROM approvals" in sql:
            params = stmt.compile().params
            ids = {v for v in params.values() if isinstance(v, uuid.UUID)}
            for (m, _), o in self.objects.items():
                if (
                    m is Approval
                    and o.subject_type == ApprovalSubjectType.trip
                    and o.subject_id in ids
                    and o.organization_id in ids
                    and o.current_status == ApprovalStatus.approved
                ):
                    return o.id
            return None
        if sql.startswith("SELECT expenses.report_id"):
            return None  # duplicate-claim lookup: the entry's own report
        if sql.startswith("SELECT max(audit_log.created_at)") or sql.startswith(
            "SELECT max(approval_steps.decided_at)"
        ):
            # Lines removed since a human return (UAT2): never returned here.
            return None
        raise AssertionError(f"unexpected scalar(): {sql}")

    def execute(self, stmt: Any) -> _Rows:
        sql = str(stmt)
        if "FROM approval_steps JOIN approvals" in sql or sql.startswith(
            "SELECT trips.primary_destination"
        ):
            # C7R re-file probes (frozen return snapshots / rejected trips): none here.
            return _Rows([])
        if sql.startswith("SELECT trips.id, trips.origin"):
            # UAT-2 Issue 4 split-trip probe (other live trips of the traveler): none
            # here; the rule itself is covered by the DB tests.
            return _Rows([])
        if sql.startswith("SELECT expenses.amount"):
            # C7R approval-time re-file probe (this report's lines / rejected lines):
            # this fake holds no rejected reports; covered by the DB tests.
            return _Rows([])
        if "JOIN approvals ON approvals.subject_id" in sql:
            # C7R SoD open-returned-report probe: this fake holds no other reports
            # by the owner; the rule itself is covered by the DB tests.
            return _Rows([])
        if "FROM expenses JOIN expense_reports" in sql:
            # Duplicate-claim lookup (C7R): this fake holds no other claims by the
            # owner; duplicate detection itself is covered by the DB tests.
            return _Rows([])
        if "FROM receipts JOIN audit_log" in sql:
            # Kept-for-audit receipt lookup (UAT2 review): none in this fake.
            return _Rows([])
        if "FROM receipts JOIN expenses" in sql:
            # Receipt-reuse lookup (UAT2): this fake holds no other claims by the
            # owner; reuse detection itself is covered by the DB tests.
            return _Rows([])
        if "FROM receipts" in sql:
            return _Rows(
                [
                    (o.id, o.expense_id)
                    for (m, _), o in self.objects.items()
                    if m is Receipt and o.expense_id is not None
                ]
            )
        if "FROM card_transactions" in sql:
            return _Rows(
                [
                    (o.id, o.expense_id)
                    for (m, _), o in self.objects.items()
                    if m is CardTransaction and o.expense_id is not None
                ]
            )
        raise AssertionError(f"unexpected execute(): {sql}")


def _trip_approval(
    s: FakeSession, trip: Trip, *, status: ApprovalStatus = ApprovalStatus.approved
) -> Approval:
    """The trip's approval row (status=approved → a HUMAN signed off on the plan)."""
    return s.add_obj(
        Approval(
            id=uuid.uuid4(),
            organization_id=trip.organization_id,
            subject_type=ApprovalSubjectType.trip,
            subject_id=trip.id,
            submitter_id=trip.traveler_id,
            current_status=status,
            is_auto_approved=status == ApprovalStatus.auto_approved,
        )
    )


def _ctx(user_id: uuid.UUID = MANAGER) -> AgentContext:
    return AgentContext(
        organization_id=ORG,
        user_id=user_id,
        granted_permission_keys=frozenset(),
        correlation_id="c7-05-test",
    )


def _session_with_users() -> FakeSession:
    s = FakeSession()
    s.add_obj(User(id=EMPLOYEE, organization_id=ORG, manager_id=MANAGER, email="e@x"))
    s.add_obj(User(id=OTHER_EMPLOYEE, organization_id=ORG, manager_id=None, email="o@x"))
    return s


def _report(
    s: FakeSession,
    *,
    total: str,
    currency: str = "USD",
    pre_submit_flags: dict | None = None,
    owner: uuid.UUID = EMPLOYEE,
) -> ExpenseReport:
    return s.add_obj(
        ExpenseReport(
            id=uuid.uuid4(),
            organization_id=ORG,
            owner_id=owner,
            title="C7-05 report",
            status=ExpenseReportStatus.submitted,
            total_amount=Decimal(total),
            currency=currency,
            pre_submit_flags=pre_submit_flags,
            trip_id=None,
        )
    )


def _entry(
    s: FakeSession,
    report: ExpenseReport,
    *,
    category: str,
    amount: str,
    vendor: str | None,
    currency: str = "USD",
    match_source: ExpenseMatchSource | None = ExpenseMatchSource.manual,
    trip_segment_id: uuid.UUID | None = None,
) -> Expense:
    return s.add_obj(
        Expense(
            id=uuid.uuid4(),
            organization_id=ORG,
            report_id=report.id,
            trip_segment_id=trip_segment_id,
            category=ExpenseCategory(category),
            amount=Decimal(amount),
            currency=currency,
            expense_date=date(2026, 11, 1),
            vendor=vendor,
            description=None,
            status=ExpenseStatus.draft,
            match_source=match_source,
            is_itemized=False,
        )
    )


def _receipt(
    s: FakeSession,
    expense: Expense | None,
    *,
    fields: dict,
    provenance: dict | None,
    uploaded_by: uuid.UUID = EMPLOYEE,
) -> Receipt:
    return s.add_obj(
        Receipt(
            id=uuid.uuid4(),
            organization_id=ORG,
            uploaded_by=uploaded_by,
            expense_id=expense.id if expense is not None else None,
            image_url="local://r.jpg",
            ocr_status=OcrStatus.processed,
            extracted_fields=fields,
            field_provenance=provenance,
        )
    )


def _trip_with_segments(
    s: FakeSession, *, traveler: uuid.UUID = EMPLOYEE, org: uuid.UUID = ORG
) -> tuple[TripSegment, TripSegment]:
    trip = s.add_obj(
        Trip(
            id=uuid.uuid4(),
            organization_id=org,
            traveler_id=traveler,
            origin="SFO",
            primary_destination="New York",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 14),
            currency="USD",
            status=TripStatus.completed,
        )
    )
    _trip_approval(s, trip)
    flight = s.add_obj(
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,
            segment_type=SegmentType.flight,
            supplier="United Airlines",
            start_datetime=datetime(2026, 6, 10, 7, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 6, 10, 15, 30, tzinfo=timezone.utc),
            location="New York, NY",
            amount=Decimal("320.00"),
            currency="USD",
            details={"cabin_class": "economy", "duration_hours": 5.5},
        )
    )
    hotel = s.add_obj(
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,
            segment_type=SegmentType.hotel,
            supplier="Marriott International",
            start_datetime=datetime(2026, 6, 10, 15, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 6, 14, 11, tzinfo=timezone.utc),
            location="New York, NY",
            amount=Decimal("956.00"),
            currency="USD",
            details={"nights": 4, "nightly_rate_usd": 239.0},
        )
    )
    return flight, hotel


def _assess(s: FakeSession, report: ExpenseReport) -> tuple[RiskAssessment, str]:
    a = assess_risk(report, ApprovalSubjectType.expense_report, context=_ctx(), session=s)  # type: ignore[arg-type]
    return a, decide_route(a)


def _violations(a: RiskAssessment, clause: str) -> list[dict]:
    return [
        f
        for f in a.flags_snapshot
        if f.get("source_clause_ref") == clause and f.get("flag_type") == "policy_violation"
    ]


# ---------------------------------------------------------------------------
# C7-05 — approval-time policy gate
# ---------------------------------------------------------------------------


class TestC705ApprovalReEvaluatesPolicy:
    def test_skybridge_air_450_without_flags_escalates(self) -> None:
        """Exact staging repro (approval 987f1b1d): 450 airfare, vendor SKYBRIDGE AIR,
        pre_submit_flags=None → was auto_approved; must escalate on AIR-PREF."""
        s = _session_with_users()
        r = _report(s, total="450.00", pre_submit_flags=None)
        _entry(s, r, category="airfare", amount="450.00", vendor="SKYBRIDGE AIR")

        a, route = _assess(s, r)

        assert route == "escalate"
        assert a.is_policy_compliant is False
        assert a.subject_in_policy is False
        assert a.blocking_flag_count >= 1
        assert _violations(a, "AIR-PREF"), a.flags_snapshot
        assert a.policy_rationale  # escalation carries a human-readable rationale

    def test_nyc_hotel_389_one_night_without_flags_escalates(self) -> None:
        """Exact staging repro (approval dc76c1e5): 389 hotel, 1 night, no flags."""
        s = _session_with_users()
        r = _report(s, total="389.00", pre_submit_flags=None)
        _entry(s, r, category="lodging", amount="389.00", vendor="C7 Grand Hotel New York")

        a, route = _assess(s, r)

        assert route == "escalate"
        assert a.is_policy_compliant is False
        assert _violations(a, "HOTEL-CAP"), a.flags_snapshot

    def test_hotel_with_receipt_location_uses_tier1_cap_and_still_escalates(self) -> None:
        """Location known from the receipt → Tier 1 cap 300; 389/night still violates."""
        s = _session_with_users()
        r = _report(s, total="389.00")
        e = _entry(s, r, category="lodging", amount="389.00", vendor="Marriott Marquis")
        _receipt(
            s,
            e,
            fields={"amount": 389.0, "vendor": "Marriott Marquis", "location": "New York, NY"},
            provenance={"vendor": "deterministic", "location": "deterministic"},
        )

        a, route = _assess(s, r)

        assert route == "escalate"
        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["cap"] == 300
        assert cap[0]["severity"] == "blocking"

    def test_clean_small_meal_auto_approves_with_explicit_compliance(self) -> None:
        """Legit path still works: 20 USD meal (under per-diem, under receipt threshold)."""
        s = _session_with_users()
        r = _report(s, total="20.00")
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        a, route = _assess(s, r)

        assert a.is_policy_compliant is True
        assert a.blocking_flag_count == 0
        assert route == "auto_approve"

    def test_preferred_airline_with_deterministic_receipt_auto_approves(self) -> None:
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(
            s,
            r,
            category="airfare",
            amount="300.00",
            vendor="United Airlines",
            match_source=ExpenseMatchSource.receipt,
        )
        _receipt(
            s,
            e,
            fields={"amount": 300.0, "vendor": "United Airlines"},
            provenance={"vendor": "deterministic"},
        )

        a, route = _assess(s, r)

        assert not _violations(a, "AIR-PREF"), a.flags_snapshot
        assert a.is_policy_compliant is True
        assert route == "auto_approve"

    def test_vendor_edited_after_ocr_is_not_trusted(self) -> None:
        """Bypass attempt: OCR read 'SkyBridge Air' deterministically; the claimant
        edits the entry vendor to 'United Airlines'.  Must NOT clear AIR-PREF."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"amount": 300.0, "vendor": "SkyBridge Air"},
            provenance={"vendor": "deterministic"},
        )

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_typed_preferred_vendor_without_evidence_is_not_trusted(self) -> None:
        """Manual entry typing 'United Airlines' (no receipt/card/segment) ≠ evidence."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_someone_elses_trip_segment_is_ignored(self) -> None:
        """Bypass attempt: link a SKYBRIDGE airfare to another traveler's United
        segment.  The foreign segment must not supply the supplier."""
        s = _session_with_users()
        flight, _ = _trip_with_segments(s, traveler=OTHER_EMPLOYEE)
        r = _report(s, total="300.00")
        _entry(
            s,
            r,
            category="airfare",
            amount="300.00",
            vendor="SKYBRIDGE AIR",
            trip_segment_id=flight.id,
        )

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_no_entries_is_not_compliant(self) -> None:
        s = _session_with_users()
        r = _report(s, total="0.00", pre_submit_flags=None)

        a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"
        assert any("no expense entries" in x for x in a.policy_rationale)

    @pytest.mark.parametrize(
        ("report_ccy", "entry_ccy"), [("USD", "KWD"), ("KWD", "KWD"), ("EUR", "USD")]
    )
    def test_non_usd_is_not_compliant(self, report_ccy: str, entry_ccy: str) -> None:
        """150 KWD (~USD 490) of meals would read as 150 USD — no FX → escalate."""
        s = _session_with_users()
        r = _report(s, total="20.00", currency=report_ccy)
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe", currency=entry_ccy)

        a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_threshold_uses_entries_sum_when_stored_total_lags(self) -> None:
        """A stale/understated report total cannot sneak a >$500 claim under the gate."""
        s = _session_with_users()
        r = _report(s, total="20.00")
        for _ in range(9):
            _entry(s, r, category="meals", amount="60.00", vendor="Cafe")  # 540 in entries

        a, route = _assess(s, r)

        assert a.subject_total == pytest.approx(540.0)
        assert route == "escalate"

    def test_malformed_segment_details_escalate(self) -> None:
        s = _session_with_users()
        flight, _ = _trip_with_segments(s)
        flight.details = ["not", "a", "dict"]  # type: ignore[assignment]
        r = _report(s, total="300.00")
        _entry(
            s,
            r,
            category="airfare",
            amount="300.00",
            vendor="United Airlines",
            trip_segment_id=flight.id,
        )

        a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_stale_clean_stored_flags_do_not_override_fresh_violation(self) -> None:
        s = _session_with_users()
        stale = {"flags": [], "evaluated_entry_count": 1, "blocking_count": 0, "advisory_count": 0}
        r = _report(s, total="450.00", pre_submit_flags=stale)
        _entry(s, r, category="airfare", amount="450.00", vendor="SKYBRIDGE AIR")

        a, route = _assess(s, r)

        assert route == "escalate"
        assert _violations(a, "AIR-PREF")

    def test_stored_blocking_flag_still_escalates_when_fresh_is_clean(self) -> None:
        s = _session_with_users()
        stored = {
            "flags": [
                {
                    "entry_ref": "Cafe",
                    "flag_type": "policy_violation",
                    "rule_id": "x",
                    "severity": "blocking",
                    "reason": "stored violation",
                }
            ],
            "blocking_count": 1,
            "advisory_count": 0,
        }
        r = _report(s, total="20.00", pre_submit_flags=stored)
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_stored_blocking_count_without_flag_rows_still_escalates(self) -> None:
        s = _session_with_users()
        r = _report(s, total="20.00", pre_submit_flags={"flags": [], "blocking_count": 2})
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        _, route = _assess(s, r)

        assert route == "escalate"

    def test_evaluation_error_is_not_compliant(self) -> None:
        s = _session_with_users()
        r = _report(s, total="20.00")
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        with patch.object(PolicyEvalTool, "evaluate_many", side_effect=RuntimeError("db down")):
            a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"
        assert any("could not be completed" in x for x in a.policy_rationale)

    def test_rule_load_error_is_not_compliant(self) -> None:
        s = _session_with_users()
        r = _report(s, total="20.00")
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        with patch.object(PolicyEvalTool, "_load_active_rules", side_effect=RuntimeError("x")):
            a, route = _assess(s, r)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_risk_summary_and_route_carry_compliance(self) -> None:
        from ai_agents.agents.approval_agent.evaluate import build_risk_summary

        s = _session_with_users()
        r = _report(s, total="450.00")
        _entry(s, r, category="airfare", amount="450.00", vendor="SKYBRIDGE AIR")
        a, route = _assess(s, r)

        summary = build_risk_summary(a, route=route)
        assert summary["is_policy_compliant"] is False
        assert summary["subject_in_policy"] is False
        assert summary["policy_rationale"]
        assert summary["routed_to_approver_id"] == str(MANAGER)


class TestC705RoutingRequiresExplicitCompliance:
    def _a(self, compliant: bool | None) -> RiskAssessment:
        return RiskAssessment(
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=EMPLOYEE,
            manager_id=MANAGER,
            subject_in_policy=True,
            blocking_flag_count=0,
            requires_approver=False,
            subject_total=100.0,
            flags_snapshot=[],
            is_policy_compliant=compliant,
        )

    def test_unknown_compliance_escalates(self) -> None:
        assert decide_route(self._a(None)) == "escalate"

    def test_false_compliance_escalates(self) -> None:
        assert decide_route(self._a(False)) == "escalate"

    def test_true_compliance_under_threshold_auto_approves(self) -> None:
        assert decide_route(self._a(True)) == "auto_approve"


# ---------------------------------------------------------------------------
# C7-15 — pre-submit resolves the real segment / entry fields
# ---------------------------------------------------------------------------


def _draft_entry(
    *,
    category: str,
    amount: float,
    vendor: str | None,
    trip_segment_id: uuid.UUID | None = None,
    primary_receipt_id: uuid.UUID | None = None,
    source_card_txn_id: uuid.UUID | None = None,
) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-06-10",
        vendor=vendor,
        description=None,
        status="matched",
        match_source="card_txn" if source_card_txn_id else None,
        trip_segment_id=str(trip_segment_id) if trip_segment_id else None,
        is_itemized=False,
        primary_receipt_id=str(primary_receipt_id) if primary_receipt_id else None,
        source_card_txn_id=str(source_card_txn_id) if source_card_txn_id else None,
        sibling_group_id=None,
    )


def _presubmit(s: FakeSession, *entries: DraftEntry) -> list:
    report = DraftReport(
        entries=list(entries),
        total_amount=sum(e.amount for e in entries),
        currency="USD",
        trip_id=None,
        title="t",
    )
    flags, _ = run_presubmit(report, context=_ctx(EMPLOYEE), session=s)  # type: ignore[arg-type]
    return flags


def _pv(flags: list, clause: str) -> list:
    return [f for f in flags if f.source_clause_ref == clause and f.flag_type == "policy_violation"]


class TestC715PresubmitUsesRealSegment:
    def test_united_flight_segment_clears_air_pref(self) -> None:
        """Repro 34176c37: United Airlines entry on a United segment → no AIR-PREF."""
        s = _session_with_users()
        flight, _ = _trip_with_segments(s)
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=flight.id,
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_four_night_nyc_marriott_is_under_tier1_cap(self) -> None:
        """Repro 34176c37: 956 over 4 nights in New York = 239/night < 300 (Tier 1)."""
        s = _session_with_users()
        _, hotel = _trip_with_segments(s)
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging",
                amount=956.0,
                vendor="New York Marriott Marquis",
                trip_segment_id=hotel.id,
            ),
        )
        assert not _pv(flags, "HOTEL-CAP"), [f.reason for f in flags]
        # nights are known from the segment → no "nights unknown" advisory
        assert not [f for f in flags if f.observed.get("nights_unknown")]

    def test_over_cap_on_real_segment_still_flags(self) -> None:
        # Product gap 2 (intended change): 310/night is only 3.3% over the Tier 1 cap and
        # the stay is a segment of the approved trip (booked through the travel tool), so
        # LOD-02 auto-approves it.  Beyond the 15% tolerance it still flags.
        s = _session_with_users()
        _, hotel = _trip_with_segments(s)
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging",
                amount=1400.0,  # 350/night over 4 nights: 16.7% over 300 (Tier 1)
                vendor="New York Marriott Marquis",
                trip_segment_id=hotel.id,
            ),
        )
        assert _pv(flags, "HOTEL-CAP")

    def test_within_lod02_tolerance_without_receipt_evidence_stays_an_exception(
        self,
    ) -> None:
        # Product gap 2, security review round 3 (intended change): a link to the hotel
        # segment alone does not prove the stay is the tool booking — LOD-02 needs a
        # receipt / card naming the booked hotel in the booked city (positive case:
        # backend/tests/test_pg2_entry_rules_db.py::test_lod02_trip_hotel_vs_unlinked).
        s = _session_with_users()
        _, hotel = _trip_with_segments(s)
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging",
                amount=1240.0,  # 310/night over 4 nights: 3.3% over 300 (Tier 1)
                vendor="New York Marriott Marquis",
                trip_segment_id=hotel.id,
            ),
        )
        (cap,) = _pv(flags, "HOTEL-CAP")
        assert cap.requires_approver == "manager"
        assert not [f for f in flags if f.flag_type == "policy_advisory"]

    def test_other_travelers_segment_is_not_used(self) -> None:
        s = _session_with_users()
        flight, _ = _trip_with_segments(s, traveler=OTHER_EMPLOYEE)
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="SKYBRIDGE AIR", trip_segment_id=flight.id
            ),
        )
        assert _pv(flags, "AIR-PREF")

    def test_other_org_segment_is_not_used(self) -> None:
        s = _session_with_users()
        flight, _ = _trip_with_segments(s, org=OTHER_ORG)
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="SKYBRIDGE AIR", trip_segment_id=flight.id
            ),
        )
        assert _pv(flags, "AIR-PREF")

    def test_no_segment_card_txn_merchant_counts_as_supplier(self) -> None:
        s = _session_with_users()
        txn = s.add_obj(
            CardTransaction(
                id=uuid.uuid4(),
                organization_id=ORG,
                user_id=EMPLOYEE,
                amount=Decimal("320.00"),
                currency="USD",
                merchant="United Airlines",
                txn_date=date(2026, 6, 10),
            )
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                source_card_txn_id=txn.id,
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_missing_airline_supplier_still_blocks(self) -> None:
        """M-A3 hard gate: an airfare with no supplier evidence must block."""
        s = _session_with_users()
        flags = _presubmit(s, _draft_entry(category="airfare", amount=320.0, vendor=None))
        pv = _pv(flags, "AIR-PREF")
        assert pv and pv[0].severity == "blocking"

    def test_receipt_location_and_nights_used_without_segment(self) -> None:
        """City known on the entry's receipt must not fall into the domestic cap."""
        s = _session_with_users()
        rec = _receipt(
            s,
            None,
            fields={
                "amount": 956.0,
                "vendor": "Marriott",
                "location": "New York, NY",
                "nights": 4,
            },
            provenance={"vendor": "deterministic", "location": "deterministic"},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging", amount=956.0, vendor="Marriott", primary_receipt_id=rec.id
            ),
        )
        assert not _pv(flags, "HOTEL-CAP"), [f.reason for f in flags]

    def test_strict_mode_raises_on_evaluation_error(self) -> None:
        s = _session_with_users()
        report = DraftReport(
            entries=[_draft_entry(category="meals", amount=20.0, vendor="Cafe")],
            total_amount=20.0,
            currency="USD",
            trip_id=None,
            title="t",
        )
        with patch.object(PolicyEvalTool, "evaluate_many", side_effect=RuntimeError("boom")):
            # Lenient (assembly) mode keeps its historical behaviour…
            flags, _ = run_presubmit(report, context=_ctx(EMPLOYEE), session=s)  # type: ignore[arg-type]
            assert flags == []
            # …strict (approval) mode surfaces the failure.
            with pytest.raises(RuntimeError):
                run_presubmit(report, context=_ctx(EMPLOYEE), session=s, strict=True)  # type: ignore[arg-type]

    def test_lenient_mode_survives_malformed_segment(self) -> None:
        """Assembly must not crash on malformed segment data (falls back to fields)."""
        s = _session_with_users()
        flight, _ = _trip_with_segments(s)
        flight.details = ["not", "a", "dict"]  # type: ignore[assignment]
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="SKYBRIDGE AIR", trip_segment_id=flight.id
            ),
        )
        assert _pv(flags, "AIR-PREF")
