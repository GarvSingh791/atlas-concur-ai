"""Product gap 2, stage 2 — review fixes (process & timing rules) on Postgres.

* EXP-04: the quarterly count includes only affidavits still IN USE — a line that got
  a receipt after signing, or a line above the affidavit ceiling (where an affidavit is
  never accepted), does not use up one of the quarter's slots;
* EXP-04: the server stamps the signing time (never the client), keeps the first one;
* trip approval fails closed: if the trip process checks crash, an otherwise
  auto-approvable trip escalates with a human-review rationale;
* PTA-05: a change that re-selects ONE leg is compared on the EFFECTIVE trip (the kept
  legs of the approved trip + the new selection), at selection and at approval;
* PTA-05: only an approved / under-review trip of the SAME route is linked as changed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
    _planner_trip,
    _presubmit,
    _seed,
    _trip_route,
)
from backend.tests.test_uat2_planner_single_trip_db import (  # noqa: F401
    _client,
    _Planner,
    planner_app,
)
from database.models.approval import Approval, ApprovalSubjectType
from database.models.expense import Expense, ExpenseCategory
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus
from database.models.trip import Trip, TripStatus
from services.mock_inventory import synthetic

pytestmark = pytest.mark.db

API = "/api/v1"


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


# ---------------------------------------------------------------------------
# EXP-04 — only affidavits in use count toward the quarter
# ---------------------------------------------------------------------------


def _line(db: Session, emp: User, amount: str, day: date) -> Expense:
    report = f.make_expense_report(db, owner=emp, title=f"R {uuid.uuid4().hex[:5]}")
    return f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.meals,
        amount=amount,
        vendor="Corner Cafe",
        expense_date=day,
    )


def _attach_receipt(db: Session, emp: User, line: Expense) -> None:
    f.make_receipt(
        db,
        uploaded_by=emp,
        expense_id=line.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={
            "amount": str(line.amount),
            "currency": "USD",
            "date": str(line.expense_date),
        },
        field_provenance={"amount": "deterministic", "currency": "deterministic"},
    )


def _affidavit_flag(db: Session, emp: User, line: Expense) -> Any:
    entry = _draft(
        "meals",
        float(str(line.amount)),
        line.expense_date,  # type: ignore[arg-type]
        entry_id=str(line.id),
        justification="Receipt lost",
        receipt_affidavit_at=_iso(line.receipt_affidavit_at),
    )
    (flag,) = [x for x in _presubmit(db, emp, entry) if x.flag_type == "missing_receipt"]
    return flag


class TestAffidavitsInUse:
    def test_a_receipted_line_and_an_over_ceiling_line_free_their_slots(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """The reviewer's setup: L1 signed then got its receipt, L2 ($80) signed over the
        ceiling, L3 a valid affidavit — L4 is only the 2nd in use, so it is accepted."""
        emp = users_a["employee"]
        _seed(db_session, org_a)
        base = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
        l1 = _line(db_session, emp, "30.00", date(2026, 10, 5))
        l1.receipt_affidavit_at = base
        l2 = _line(db_session, emp, "80.00", date(2026, 10, 6))
        l2.receipt_affidavit_at = base + timedelta(hours=1)
        l3 = _line(db_session, emp, "30.00", date(2026, 10, 7))
        l3.receipt_affidavit_at = base + timedelta(hours=2)
        l4 = _line(db_session, emp, "30.00", date(2026, 11, 2))
        l4.receipt_affidavit_at = base + timedelta(days=1)
        l4.justification = "Receipt lost"
        db_session.flush()
        _attach_receipt(db_session, emp, l1)
        db_session.flush()

        flag = _affidavit_flag(db_session, emp, l4)
        assert flag.severity == "advisory", flag.reason
        assert flag.observed["affidavit_accepted"] is True
        assert flag.observed["affidavits_before_in_quarter"] == 1
        assert "affidavit 2 of 3" in flag.reason

    def test_three_signed_one_receipted_then_a_fourth_is_accepted(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        base = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
        lines = []
        for i in range(3):
            line = _line(db_session, emp, "30.00", date(2026, 10, 5 + i))
            line.receipt_affidavit_at = base + timedelta(hours=i)
            lines.append(line)
        fourth = _line(db_session, emp, "30.00", date(2026, 11, 2))
        fourth.receipt_affidavit_at = base + timedelta(days=1)
        db_session.flush()
        # Control: all three still in use -> the 4th is the 4th of the quarter.
        flag = _affidavit_flag(db_session, emp, fourth)
        assert flag.severity == "blocking" and flag.observed["affidavit_issue"] == "over_count"

        _attach_receipt(db_session, emp, lines[1])  # the receipt turned up
        db_session.flush()
        flag = _affidavit_flag(db_session, emp, fourth)
        assert flag.severity == "advisory", flag.reason
        assert flag.observed["affidavits_before_in_quarter"] == 2
        assert "affidavit 3 of 3" in flag.reason


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class TestServerStamp:
    def test_signing_stamps_server_time_and_keeps_the_first(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        employee_client: TestClient,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        line = _line(db_session, emp, "40.00", TODAY)
        started = datetime.now(timezone.utc)
        r = _patch(
            employee_client,
            db_session,
            line,
            {"receipt_affidavit": True, "justification": "Receipt lost"},
        )
        assert r.status_code == 200, r.text
        first = _ts(r.json()["receipt_affidavit_at"])
        assert started <= first <= datetime.now(timezone.utc)

        # Re-signing keeps the first signing time (the quarter order never moves).
        r = _patch(employee_client, db_session, line, {"receipt_affidavit": True})
        assert r.status_code == 200, r.text
        assert _ts(r.json()["receipt_affidavit_at"]) == first

        # A client-supplied time is never taken.
        r = _patch(
            employee_client,
            db_session,
            line,
            {"receipt_affidavit_at": "2001-01-01T00:00:00+00:00"},
        )
        db_session.expire_all()
        stored = db_session.get(Expense, line.id)
        assert stored is not None and stored.receipt_affidavit_at == first, r.text


# ---------------------------------------------------------------------------
# Trip approval — fail closed when the process checks crash
# ---------------------------------------------------------------------------


class TestTripApprovalFailClosed:
    def test_crashed_trip_process_checks_escalate(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        emp = users_a["employee"]
        _seed(db_session, org_a)
        # Control: in policy, under $500, 14 days ahead -> auto-approved.
        _, route = _trip_route(
            db_session, _planner_trip(db_session, emp, depart=TODAY + timedelta(days=14))
        )
        assert route == "auto_approve"

        def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("process checks unavailable")

        monkeypatch.setattr(process_checks, "trip_flags", _boom)
        trip = _planner_trip(db_session, emp, depart=TODAY + timedelta(days=15))
        db_session.expire_all()
        fresh = db_session.get(Trip, trip.id)
        assert fresh is not None
        a = assess_risk(fresh, ApprovalSubjectType.trip, context=_ctx(emp), session=db_session)
        assert decide_route(a) == "escalate"
        assert "Trip process checks could not be completed; human review required." in str(
            a.policy_rationale
        )


# ---------------------------------------------------------------------------
# PTA-05 — effective trip estimate, linking
# ---------------------------------------------------------------------------


def _pick(options: list[dict], supplier: str, cabin: str | None = None) -> dict:
    return next(
        o
        for o in options
        if o["supplier"] == supplier and (cabin is None or o.get("cabin_class") == cabin)
    )


def _submitted_trip(p: _Planner, client: TestClient) -> tuple[dict, list[dict], list[dict], Any]:
    """Delta economy + Marriott, submitted for approval (ORD → JFK)."""
    flights, hotels = p.options()
    delta = _pick(flights, "Delta Air Lines", "economy")
    marriott = next(h for h in hotels if "Marriott" in h["supplier"])
    first = p.send("Plan my trip", selected_option_ids=[delta["option_id"], marriott["option_id"]])
    r = client.patch(f"{API}/trips/{first['trip_id']}", json={"status": "pending_approval"})
    assert r.status_code == 200 and r.json()["status"] == "pending_approval", r.text
    return first, flights, hotels, Decimal(str(r.json()["estimated_cost"]))


class TestPta05EffectiveTrip:
    def test_replacing_only_the_flight_counts_the_kept_hotel(
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
        first, flights, _hotels, approved = _submitted_trip(p, client)
        business = _pick(flights, "United Airlines", "business")
        delta = _pick(flights, "Delta Air Lines", "economy")
        # Preconditions of the reviewer's case: the new flight alone is BELOW the approved
        # total, the effective trip (flight + kept hotel) is far above it.
        hotel_total = approved - Decimal(str(delta["amount_usd"]))
        effective = Decimal(str(business["amount_usd"])) + hotel_total
        assert Decimal(str(business["amount_usd"])) < approved
        assert (effective - approved) * 100 > 10 * approved

        second = p.select(business["option_id"])
        change = second["trip_change"]
        assert change["changes_trip_id"] == first["trip_id"]
        assert change["reapproval_required"] is True, second["trip_message"]
        assert "must be re-approved" in second["trip_message"]
        assert "within the re-approval threshold" not in second["trip_message"]
        assert f"${effective:,.2f}" in second["trip_message"]

        r = client.patch(f"{API}/trips/{second['trip_id']}", json={"status": "pending_approval"})
        assert r.status_code == 200, r.text
        approval = db_session.scalar(
            sa.select(Approval).where(Approval.subject_id == uuid.UUID(second["trip_id"]))
        )
        assert approval is not None
        (flag,) = _clause((approval.risk_summary or {})["flags"], "PTA-05")
        assert flag["flag_type"] == "approval_required"
        assert flag["observed"]["new_estimate"] == str(effective)
        assert flag["related_subject_id"] == first["trip_id"]

    def test_a_small_single_leg_change_needs_no_reapproval(
        self,
        planner_app: Any,  # noqa: F811
        rbac_catalog: None,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(process_checks, "today_utc", date.today)
        # Mock fares carry a per-day jitter (+-8%); pin it so the fares are the base
        # fares every day and this case does not depend on the date it runs.
        monkeypatch.setattr(synthetic, "_jitter", lambda *_a, **_k: 1.0)
        emp = users_a["employee"]
        _seed(db_session, org_a)
        client = _client(planner_app, emp)
        p = _Planner(client)
        first, flights, _hotels, approved = _submitted_trip(p, client)
        delta = _pick(flights, "Delta Air Lines", "economy")
        # Take whichever economy flight makes the trip cost a little MORE than approved
        # (within the 10% re-approval threshold).
        kept = approved - Decimal(str(delta["amount_usd"]))

        def _small_increase(o: dict) -> bool:
            eff = kept + Decimal(str(o["amount_usd"]))
            return eff > approved and (eff - approved) * 100 <= 10 * approved

        candidates = [
            o
            for o in flights
            if o.get("cabin_class") == "economy"
            and o["option_id"] != delta["option_id"]
            and _small_increase(o)
        ]
        assert candidates, "the base fares offer no economy flight within +10% of Delta"
        united = min(candidates, key=lambda o: Decimal(str(o["amount_usd"])))
        effective = kept + Decimal(str(united["amount_usd"]))
        assert effective > approved and (effective - approved) * 100 <= 10 * approved

        second = p.select(united["option_id"])
        change = second["trip_change"]
        assert change["changes_trip_id"] == first["trip_id"]
        assert change["reapproval_required"] is False, second["trip_message"]
        assert "within the re-approval threshold" in second["trip_message"]

        r = client.patch(f"{API}/trips/{second['trip_id']}", json={"status": "pending_approval"})
        assert r.status_code == 200, r.text
        approval = db_session.scalar(
            sa.select(Approval).where(Approval.subject_id == uuid.UUID(second["trip_id"]))
        )
        flags = (approval.risk_summary or {}).get("flags", []) if approval else []
        assert _clause(flags, "PTA-05") == []


class TestPta05Linking:
    @pytest.mark.parametrize(
        "state", [TripStatus.rejected, TripStatus.cancelled, TripStatus.completed]
    )
    def test_a_finished_trip_is_not_changed(
        self,
        planner_app: Any,  # noqa: F811
        rbac_catalog: None,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        monkeypatch: pytest.MonkeyPatch,
        state: TripStatus,
    ) -> None:
        monkeypatch.setattr(process_checks, "today_utc", date.today)
        emp = users_a["employee"]
        _seed(db_session, org_a)
        client = _client(planner_app, emp)
        p = _Planner(client)
        first, flights, _hotels, _approved = _submitted_trip(p, client)
        prior = db_session.get(Trip, uuid.UUID(first["trip_id"]))
        assert prior is not None
        prior.status = state
        db_session.flush()

        second = p.select(_pick(flights, "United Airlines", "business")["option_id"])
        assert second["trip_id"] != first["trip_id"]
        assert (second.get("trip_change") or {}).get("changes_trip_id") is None
        assert "PTA-05" not in second["trip_message"]
        new_trip = db_session.get(Trip, uuid.UUID(second["trip_id"]))
        assert new_trip is not None
        assert "changes_trip_id" not in (new_trip.recommendation_context or {})

    def test_a_different_route_is_another_trip_not_a_change(
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
        first, _flights, _hotels, _approved = _submitted_trip(p, client)

        out = p.send("Update my trip plan", origin="SFO")
        sfo = next(
            o
            for o in out["shortlist"]
            if o["segment_type"] == "flight" and o["cabin_class"] == "business"
        )
        second = p.send("Plan my trip", origin="SFO", selected_option_ids=[sfo["option_id"]])
        assert second["trip_id"] != first["trip_id"]
        assert (second.get("trip_change") or {}).get("changes_trip_id") is None
        assert "PTA-05" not in second["trip_message"]
        new_trip = db_session.get(Trip, uuid.UUID(second["trip_id"]))
        assert new_trip is not None
        assert "changes_trip_id" not in (new_trip.recommendation_context or {})
