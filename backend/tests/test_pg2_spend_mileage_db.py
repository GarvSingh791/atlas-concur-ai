"""Product gap 2 (policy2) — spend caps, gift disclosure and mileage, end to end (DB).

* POST /expense-reports/{id}/mileage: owner-only, draft-only, amount COMPUTED
  server-side (business miles x the org's MIL-01 rate, Decimal); a client amount
  that disagrees is a 422; audited; no receipt needed (REF-08).
* PATCH /expenses/{id}: attendees / disclosure / mileage are category-gated; a
  mileage amount can never be set by the client; a trip edit recomputes it.
* Pre-submit + approval-time evaluation cite MEAL-05 / ENT-02 / ENT-04 / MIL-0x;
  MIL-03 and government officials reach a human (never auto-approved).
* Migration 0019's data step: idempotent inserts with audit rows, admin-edited
  rules untouched, fail-closed downgrade.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.context import AgentContext
from database.models.approval import ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import POLICY_NAME, RULES

pytestmark = pytest.mark.db

API = "/api/v1"
TRIP: dict[str, Any] = {
    "expense_date": "2026-06-10",
    "distance": "120.5",
    "unit": "mi",
    "origin": "Boston office",
    "destination": "Client site, Providence",
    "round_trip": True,
    "commute_offset": "20",
}
_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "0019_spend_caps_mileage.py"
)


def _mig() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0019_db", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def policy(db_session: Session, org_a: Any) -> Policy:
    """Every seeded rule (the stub's 7 + product gap 2's 6), active."""
    pol = f.make_policy(db_session, org=org_a, is_active=True)
    for r in RULES:
        f.make_policy_rule(
            db_session,
            policy=pol,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
    return pol


def _report(db: Session, owner: User, **kw: Any) -> ExpenseReport:
    report = f.make_expense_report(db, owner=owner, title="June trip", **kw)
    db.flush()
    return report


def _add(client: TestClient, report: ExpenseReport, body: dict, version: int | None = None) -> Any:
    # Integration hardening: POST /mileage needs If-Match or an Idempotency-Key (428
    # without either).  A call without a version here is one deliberate submit, so it
    # carries a fresh key (test_pg_integ_policy_hardening_db covers the 428 / replay).
    headers = (
        {"If-Match": str(version)}
        if version is not None
        else {"Idempotency-Key": f"test-{uuid.uuid4()}"}
    )
    return client.post(f"{API}/expense-reports/{report.id}/mileage", json=body, headers=headers)


def _flags(client: TestClient, report: ExpenseReport, clause: str) -> list[dict]:
    r = client.get(f"{API}/expense-reports/{report.id}")
    assert r.status_code == 200, r.text
    snap = r.json()["pre_submit_flags"] or {}
    return [fl for fl in snap.get("flags", []) if fl.get("source_clause_ref") == clause]


def _all_flags(client: TestClient, report: ExpenseReport, entry_id: str) -> list[dict]:
    r = client.get(f"{API}/expense-reports/{report.id}")
    snap = r.json()["pre_submit_flags"] or {}
    return [fl for fl in snap.get("flags", []) if fl.get("entry_id") == entry_id]


def _patch(client: TestClient, entry_id: Any, body: dict) -> Any:
    return client.patch(f"{API}/expenses/{entry_id}", json=body)


# ---------------------------------------------------------------------------
# POST /expense-reports/{id}/mileage
# ---------------------------------------------------------------------------


def test_add_mileage_computes_the_amount_server_side(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    r = _add(employee_client, report, TRIP)
    assert r.status_code == 201, r.text
    body = r.json()
    # (120.5 x 2 - 20) = 221.0 business miles x $0.70 = $154.70
    assert body["category"] == "mileage" and body["currency"] == "USD"
    assert Decimal(body["amount"]) == Decimal("154.70")
    trip = body["mileage"]
    assert trip["billable_miles"] == "221.0" and trip["rate_per_mile"] == "0.70"
    assert trip["rate_clause"] == "MIL-01" and trip["origin"] == "Boston office"
    db_session.refresh(report)
    assert report.total_amount == Decimal("154.70")
    entry = db_session.get(Expense, body["id"])
    assert entry is not None and entry.amount == Decimal("154.70")
    audit = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_id == entry.id, AuditLog.action == "expense.created"
        )
    ).one()
    assert audit.actor_id == users_a["employee"].id
    assert (audit.after_state or {})["mileage"]["billable_miles"] == "221.0"
    # REF-08: no receipt, and nothing blocks this compliant line.
    flags = _all_flags(employee_client, report, body["id"])
    assert not [fl for fl in flags if fl["severity"] == "blocking"], flags
    assert not [fl for fl in flags if fl["flag_type"] == "missing_receipt"], flags


def test_add_mileage_refuses_an_amount_that_disagrees(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    r = _add(employee_client, report, {**TRIP, "amount": "999.00"})
    assert r.status_code == 422, r.text
    assert "computed" in r.json()["error"]["message"]
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(Expense).where(Expense.report_id == report.id)
    )
    assert count == 0
    ok = _add(employee_client, report, {**TRIP, "amount": "154.70"})
    assert ok.status_code == 201, ok.text


@pytest.mark.parametrize(
    "patch",
    [
        {"rate_per_mile": "5.00"},  # the client never chooses the rate
        {"distance": "0"},
        {"distance": "10.25"},  # 0.1-mile precision
        {"distance": "10001"},
        {"commute_offset": None},  # MIL-02: required (0 allowed)
        {"origin": "   "},
        {"unit": "furlongs"},
        {"commute_offset": "300"},  # nothing left to reimburse
    ],
)
def test_add_mileage_validates_the_trip(
    patch: dict,
    policy: Policy,
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    report = _report(db_session, users_a["employee"])
    r = _add(employee_client, report, {**TRIP, **patch})
    assert r.status_code == 422, r.text


def test_add_mileage_is_owner_only_and_draft_only(
    policy: Policy,
    employee_client: TestClient,
    manager_client: TestClient,
    employee_client_b: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    report = _report(db_session, users_a["employee"])
    assert _add(manager_client, report, TRIP).status_code == 403  # team read, no write
    assert _add(employee_client_b, report, TRIP).status_code == 404  # other org
    assert _add(employee_client, report, TRIP, version=report.version + 7).status_code == 409
    submitted = _report(db_session, users_a["employee"], status=ExpenseReportStatus.submitted)
    assert _add(employee_client, submitted, TRIP).status_code == 409


def test_the_rate_is_the_active_mil01_rule_and_a_rate_change_flags_old_lines(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    short = {**TRIP, "distance": "10", "round_trip": False, "commute_offset": "0"}
    first = _add(employee_client, report, short).json()
    assert Decimal(first["amount"]) == Decimal("7.00")
    rule = db_session.scalars(
        sa.select(PolicyRule).where(
            PolicyRule.policy_id == policy.id, PolicyRule.source_clause_ref == "MIL-01"
        )
    ).one()
    rule.definition = {**rule.definition, "rate_per_mile": "0.725"}
    db_session.flush()
    second = _add(employee_client, report, short).json()
    assert Decimal(second["amount"]) == Decimal("7.25")
    stale = [fl for fl in _flags(employee_client, report, "MIL-01") if fl["severity"] == "blocking"]
    assert [fl["entry_id"] for fl in stale] == [first["id"]]
    # Saving the trip again recomputes it at the current rate.
    trip = {k: v for k, v in short.items() if k != "expense_date"}
    r = _patch(employee_client, first["id"], {"mileage": trip})
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["amount"]) == Decimal("7.25")
    assert not [
        fl for fl in _flags(employee_client, report, "MIL-01") if fl["severity"] == "blocking"
    ]


# ---------------------------------------------------------------------------
# PATCH /expenses/{id}
# ---------------------------------------------------------------------------


def test_a_mileage_amount_can_never_be_set_by_the_client(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    entry = _add(employee_client, report, TRIP).json()
    r = _patch(employee_client, entry["id"], {"amount": "1.00"})
    assert r.status_code == 422, r.text
    same = _patch(employee_client, entry["id"], {"amount": "154.70", "description": "Visit"})
    assert same.status_code == 200 and Decimal(same.json()["amount"]) == Decimal("154.70")
    assert _patch(employee_client, entry["id"], {"category": "meals"}).status_code == 422
    assert _patch(employee_client, entry["id"], {"currency": "EUR"}).status_code == 422
    db_session.expire_all()
    assert db_session.get(Expense, entry["id"]).amount == Decimal("154.70")  # type: ignore[union-attr]


def test_a_trip_edit_cannot_relabel_the_usd_mileage_amount_in_another_currency(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """Review fix: a PATCH carrying both a trip and a currency used to store the
    USD-computed amount as EUR 154.70 (the trip branch never checked currency)."""
    report = _report(db_session, users_a["employee"])
    entry = _add(employee_client, report, TRIP).json()
    trip = {k: v for k, v in TRIP.items() if k != "expense_date"}
    r = _patch(employee_client, entry["id"], {"mileage": trip, "currency": "EUR"})
    assert r.status_code == 422, r.text
    assert "USD" in r.text
    ok = _patch(employee_client, entry["id"], {"mileage": trip, "currency": "USD"})
    assert ok.status_code == 200 and ok.json()["currency"] == "USD", ok.text
    db_session.expire_all()
    stored = db_session.get(Expense, entry["id"])
    assert stored is not None
    assert stored.currency == "USD" and stored.amount == Decimal("154.70")
    # Defence in depth: a row relabelled before this fix is blocked by MIL-01.
    stored.currency = "EUR"
    db_session.flush()
    # Any write re-runs the pre-submit check (the snapshot GET returns).
    assert _patch(employee_client, entry["id"], {"description": "Visit"}).status_code == 200
    blocking = [
        fl for fl in _flags(employee_client, report, "MIL-01") if fl["severity"] == "blocking"
    ]
    assert [fl["entry_id"] for fl in blocking] == [entry["id"]]


def test_no_other_path_creates_a_mileage_entry(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    body = {
        "title": "Drive",
        "currency": "USD",
        "entries": [
            {
                "category": "mileage",
                "amount": "500.00",
                "currency": "USD",
                "expense_date": "2026-06-10",
            }
        ],
    }
    assert employee_client.post(f"{API}/expense-reports", json=body).status_code == 422
    report = _report(db_session, users_a["employee"])
    meal = f.make_expense(db_session, report=report, category=ExpenseCategory.meals, amount="20")
    r = _patch(employee_client, meal.id, {"category": "mileage"})
    assert r.status_code == 422
    r = _patch(
        employee_client,
        meal.id,
        {"mileage": {k: v for k, v in TRIP.items() if k != "expense_date"}},
    )
    assert r.status_code == 422


def test_team_meal_attendees_switch_from_per_diem_to_meal05(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    meal = f.make_expense(
        db_session, report=report, category=ExpenseCategory.meals, amount="200.00"
    )
    r = _patch(employee_client, meal.id, {"description": "Team dinner"})
    assert r.status_code == 200
    assert [fl for fl in _flags(employee_client, report, "MEALS-PD")]
    assert not _flags(employee_client, report, "MEAL-05")
    # Two people, Tier 3: $60 x 2 = $120 < $200 -> MEAL-05 violation, no per diem.
    r = _patch(employee_client, meal.id, {"attendee_count": 2})
    assert r.status_code == 200 and r.json()["attendee_count"] == 2
    [flag] = _flags(employee_client, report, "MEAL-05")
    assert flag["severity"] == "blocking" and flag["justifiable"] is True
    assert "MEAL-05" in flag["reason"]
    assert not _flags(employee_client, report, "MEALS-PD")
    # Four people: $240 >= $200 -> within the cap, but the (unverified) attendee
    # count still reaches the approver as an advisory (review fix).
    assert _patch(employee_client, meal.id, {"attendee_count": 4}).status_code == 200
    [within] = _flags(employee_client, report, "MEAL-05")
    assert within["flag_type"] == "approval_required" and within["severity"] == "advisory"
    # Attendees only fit meals / entertainment lines.
    assert _patch(employee_client, meal.id, {"attendee_count": 0}).status_code == 422
    hotel = f.make_expense(db_session, report=report, category=ExpenseCategory.lodging)
    assert _patch(employee_client, hotel.id, {"attendee_count": 3}).status_code == 422
    # Changing the category away clears the stale count.
    r = _patch(employee_client, meal.id, {"category": "other"})
    assert r.status_code == 200 and r.json()["attendee_count"] is None


def test_gift_over_100_blocks_until_disclosed_and_the_disclosure_is_audited(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    gift = f.make_expense(db_session, report=report, category=ExpenseCategory.gift, amount="150")
    assert _patch(employee_client, gift.id, {"description": "Wine"}).status_code == 200
    [flag] = _flags(employee_client, report, "ENT-04")
    assert flag["severity"] == "blocking" and flag["justifiable"] is False
    r = _patch(employee_client, gift.id, {"justification": "Small token"})
    assert [fl["severity"] for fl in _flags(employee_client, report, "ENT-04")] == ["blocking"]
    disclosure = {
        "recipient": "Pat Client",
        "recipient_organization": "Acme",
        "business_purpose": "Renewal thank-you",
        "government_official": False,
    }
    r = _patch(employee_client, gift.id, {"disclosure": disclosure})
    assert r.status_code == 200 and r.json()["disclosure"] == disclosure
    assert not _flags(employee_client, report, "ENT-04")
    audits = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_id == gift.id, AuditLog.action == "expense.updated"
        )
    ).all()
    disclosed = [a for a in audits if (a.after_state or {}).get("disclosure") == disclosure]
    assert len(disclosed) == 1 and (disclosed[0].before_state or {})["disclosure"] is None
    meal = f.make_expense(db_session, report=report, category=ExpenseCategory.meals)
    assert _patch(employee_client, meal.id, {"disclosure": disclosure}).status_code == 422


def test_long_trips_and_government_officials_always_reach_a_human(
    policy: Policy,
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    report = _report(db_session, users_a["employee"])
    long = {**TRIP, "distance": "300.1", "round_trip": False, "commute_offset": "0"}
    entry = _add(employee_client, report, long).json()
    [flag] = _flags(employee_client, report, "MIL-03")
    assert flag["flag_type"] == "approval_required" and flag["requires_approver"] == "manager"
    assert flag["entry_id"] == entry["id"]
    db_session.refresh(report)
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )
    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)
    assert risk.requires_approver is True and decide_route(risk) == "escalate"

    gift_report = _report(db_session, users_a["employee"])
    gift = f.make_expense(
        db_session,
        report=gift_report,
        category=ExpenseCategory.gift,
        amount="20",
        disclosure={"government_official": True},
    )
    assert _patch(employee_client, gift.id, {"description": "Pen set"}).status_code == 200
    [gflag] = _flags(employee_client, gift_report, "ENT-04")
    assert gflag["requires_approver"] == "Legal/Compliance"
    db_session.refresh(gift_report)
    risk = assess_risk(
        gift_report, ApprovalSubjectType.expense_report, context=ctx, session=db_session
    )
    assert decide_route(risk) == "escalate"


# ---------------------------------------------------------------------------
# Migration 0019 data step
# ---------------------------------------------------------------------------


def _stub_policy(db: Session, org: Any) -> Policy:
    stub = f.make_policy(db, org=org, name=POLICY_NAME, is_active=True)
    for r in RULES[:7]:  # what an existing (pre-0019) database carries
        f.make_policy_rule(
            db,
            policy=stub,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
    return stub


def _rules(db: Session, policy: Policy) -> dict[str, PolicyRule]:
    db.expire_all()
    rows = db.scalars(sa.select(PolicyRule).where(PolicyRule.policy_id == policy.id)).all()
    return {r.source_clause_ref or "": r for r in rows}


def test_migration_inserts_the_rules_once_with_audit_rows(
    db_session: Session, org_a: Any, org_b: Any
) -> None:
    mig = _mig()
    stub_a = _stub_policy(db_session, org_a)
    stub_b = _stub_policy(db_session, org_b)
    other = f.make_policy(db_session, org=org_a, name="Some other policy")
    conn = db_session.connection()
    # >= : a database another test committed a seeded org into may hold more stubs.
    assert mig.apply_spend_rules(conn) >= 12
    assert mig.apply_spend_rules(conn) == 0  # idempotent
    for stub in (stub_a, stub_b):
        rules = _rules(db_session, stub)
        for ref, rule_type, applies_to, definition in mig.NEW_RULES:
            row = rules[ref]
            assert row.rule_type.value == rule_type and row.applies_to.value == applies_to
            assert row.definition == definition and row.status == PolicyRuleStatus.active
    assert not _rules(db_session, other)
    inserted = {
        r.id
        for stub in (stub_a, stub_b)
        for r in _rules(db_session, stub).values()
        if r.rule_type.value in mig.NEW_RULE_TYPES
    }
    audits = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "policy_rule.created", AuditLog.entity_id.in_(inserted)
        )
    ).all()
    assert len(inserted) == 12 and len(audits) == 12 and all(a.actor_id is None for a in audits)
    assert {a.organization_id for a in audits} == {org_a.id, org_b.id}


def test_migration_never_touches_an_admin_edited_rule(db_session: Session, org_a: Any) -> None:
    mig = _mig()
    stub = _stub_policy(db_session, org_a)
    custom = f.make_policy_rule(
        db_session,
        policy=stub,
        rule_type=PolicyRuleType.mileage,
        applies_to=PolicyAppliesTo.all,
        definition={"check": "rate", "rate_per_mile": "0.67", "expense_categories": ["mileage"]},
        source_clause_ref="MIL-01",
        status=PolicyRuleStatus.archived,
    )
    assert mig.apply_spend_rules(db_session.connection()) >= 5
    rules = _rules(db_session, stub)
    assert rules["MIL-01"].id == custom.id
    assert rules["MIL-01"].definition["rate_per_mile"] == "0.67"
    assert rules["MIL-01"].status == PolicyRuleStatus.archived


def test_migration_downgrade_is_audited_and_refuses_while_new_categories_are_used(
    db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    mig = _mig()
    stub = _stub_policy(db_session, org_a)
    conn = db_session.connection()
    mig.apply_spend_rules(conn)
    report = _report(db_session, users_a["employee"])
    gift = f.make_expense(db_session, report=report, category=ExpenseCategory.gift)
    with pytest.raises(RuntimeError, match="downgrade refused"):
        with db_session.begin_nested():
            mig.remove_spend_rules(db_session.connection())
    db_session.delete(gift)
    db_session.flush()
    mine = {
        r.id
        for ref, r in _rules(db_session, stub).items()
        if r.rule_type.value in mig.NEW_RULE_TYPES
    }
    assert len(mine) == 6
    # Every new-type rule in the database goes (other orgs' too: pre-0019 code
    # cannot load any of them).
    assert mig.remove_spend_rules(db_session.connection()) >= 6
    assert set(_rules(db_session, stub)) == {r["clause_ref"] for r in RULES[:7]}
    deleted = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "policy_rule.deleted", AuditLog.entity_id.in_(mine)
        )
    ).all()
    assert len(deleted) == 6
    assert {(a.before_state or {})["source_clause_ref"] for a in deleted} == {
        "MEAL-05",
        "ENT-02",
        "ENT-04",
        "MIL-01",
        "MIL-02",
        "MIL-03",
    }


# ---------------------------------------------------------------------------
# A team meal never counts toward the claimant's own per-diem day total
# ---------------------------------------------------------------------------


def _segment_total_with_team_meals(db_session: Session, org_a: Any, owner: User) -> float | None:
    """A trip with the owner's $40 meal, a persisted $300 team dinner (5 people), a
    persisted $25 solo meal and an unsaved $500 team-meal peer (3 people)."""
    from ai_agents.agents.expense_agent.presubmit import (  # noqa: PLC0415
        _segment_meal_total,
        draft_entries_for_expenses,
    )

    trip = f.make_trip(db_session, org=org_a, traveler=owner)
    seg = f.make_trip_segment(db_session, trip=trip)
    report = _report(db_session, owner)
    mine = f.make_expense(
        db_session,
        report=report,
        category=ExpenseCategory.meals,
        amount="40",
        trip_segment_id=seg.id,
    )
    f.make_expense(  # persisted team dinner on the same trip (not a peer)
        db_session,
        report=report,
        category=ExpenseCategory.meals,
        amount="300",
        trip_segment_id=seg.id,
        attendee_count=5,
    )
    f.make_expense(  # persisted solo meal on the same trip: counts
        db_session,
        report=report,
        category=ExpenseCategory.meals,
        amount="25",
        trip_segment_id=seg.id,
        attendee_count=1,
    )
    [draft] = draft_entries_for_expenses([mine], session=db_session, organization_id=org_a.id)
    team_peer = dataclasses.replace(draft, entry_id=None, amount=500.0, attendee_count=3)
    ctx = AgentContext(
        organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=owner.id
    )
    return _segment_meal_total(
        draft,
        {seg.id},
        peers=[draft, team_peer],
        context=ctx,
        session=db_session,
        owner_id=owner.id,
    )


def test_team_meals_leave_the_per_diem_day_total(
    policy: Policy, db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    total = _segment_total_with_team_meals(db_session, org_a, users_a["employee"])
    assert total == 65.0  # 40 + 25; the 300 and 500 team meals are excluded (MEAL-05)


def test_team_meals_stay_in_the_per_diem_total_without_an_active_meal05(
    policy: Policy, db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    """Review fix (fail closed): with MEAL-05 archived nothing caps a team meal per
    head, so a typed attendee count never takes it out of the per-diem total."""
    meal05 = db_session.scalars(
        sa.select(PolicyRule).where(
            PolicyRule.policy_id == policy.id, PolicyRule.source_clause_ref == "MEAL-05"
        )
    ).one()
    meal05.status = PolicyRuleStatus.archived
    db_session.flush()
    total = _segment_total_with_team_meals(db_session, org_a, users_a["employee"])
    assert total == 865.0  # 40 + 300 + 25 + 500


# ---------------------------------------------------------------------------
# Review fixes: receipts never reach a mileage entry; attendee counts reach a human
# ---------------------------------------------------------------------------


def _processed_receipt(db: Session, owner: User, entry: Any = None, **fields: Any) -> Any:
    from database.models.receipt import OcrStatus  # noqa: PLC0415

    extracted = {"vendor": "Shell", "amount": 154.70, "date": "2026-06-10", "currency": "USD"}
    return f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id if entry is not None else None,
        ocr_status=OcrStatus.processed,
        image_url=f"{owner.organization_id}/pg2-{extracted['vendor']}",
        extracted_fields={**extracted, **fields},
        field_provenance={"vendor": "deterministic", "amount": "deterministic"},
    )


def _correct(client: TestClient, receipt: Any, field: str, value: Any) -> Any:
    return client.patch(f"{API}/receipts/{receipt.id}", json={"field": field, "value": value})


def test_a_receipt_cannot_be_attached_to_a_mileage_entry(
    app: Any,
    policy: Policy,
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    from services.storage_provider.factory import get_storage_provider  # noqa: PLC0415

    report = _report(db_session, users_a["employee"])
    entry = _add(employee_client, report, TRIP).json()
    receipt = _processed_receipt(db_session, users_a["employee"])
    r = employee_client.post(
        f"{API}/expenses/{entry['id']}/receipts", json={"receipt_id": str(receipt.id)}
    )
    assert r.status_code == 422, r.text
    assert "REF-08" in r.json()["error"]["message"]
    db_session.expire_all()
    assert db_session.get(type(receipt), receipt.id).expense_id is None  # type: ignore[union-attr]

    stored: list[str] = []

    class _Storage:
        def put_object(self, key: str, *_a: Any, **_k: Any) -> None:
            stored.append(key)

    app.dependency_overrides[get_storage_provider] = lambda: _Storage()
    try:
        up = employee_client.post(
            f"{API}/receipts",
            params={"expense_id": entry["id"]},
            files={"file": ("r.jpg", b"img", "image/jpeg")},
        )
    finally:
        app.dependency_overrides.pop(get_storage_provider, None)
    assert up.status_code == 422, up.text
    assert stored == []  # refused before anything was stored


def test_a_receipt_correction_never_rewrites_a_mileage_entry(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """Defence in depth: should a receipt ever back a mileage entry (a link made
    before the attach guard), correcting it changes the receipt, not the entry."""
    report = _report(db_session, users_a["employee"])
    body = _add(employee_client, report, TRIP).json()
    entry = db_session.get(Expense, body["id"])
    assert entry is not None
    trip = dict(entry.mileage or {})
    receipt = _processed_receipt(db_session, users_a["employee"], entry)
    for field, value in (("amount", 999.99), ("currency", "EUR"), ("expense_type", "meals")):
        r = _correct(employee_client, receipt, field, value)
        assert r.status_code == 200, r.text
        assert r.json()["linked_expense_sync"] == "not_applicable", (field, r.json())
    db_session.expire_all()
    entry = db_session.get(Expense, body["id"])
    assert entry is not None
    assert entry.amount == Decimal("154.70") and entry.currency == "USD"
    assert entry.category == ExpenseCategory.mileage and entry.mileage == trip
    db_session.refresh(report)
    assert report.total_amount == Decimal("154.70")
    assert not [
        fl for fl in _flags(employee_client, report, "MIL-01") if fl["severity"] == "blocking"
    ]


def test_a_receipt_category_correction_clears_fields_that_no_longer_fit(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    meal = f.make_expense(
        db_session, report=report, category=ExpenseCategory.meals, amount="154.70"
    )
    assert _patch(employee_client, meal.id, {"attendee_count": 3}).status_code == 200
    receipt = _processed_receipt(db_session, users_a["employee"], meal)
    r = _correct(employee_client, receipt, "expense_type", "other")
    assert r.status_code == 200 and r.json()["linked_expense_sync"] == "updated", r.text
    db_session.expire_all()
    fresh = db_session.get(Expense, meal.id)
    assert fresh is not None and fresh.category == ExpenseCategory.other
    assert fresh.attendee_count is None

    gift = f.make_expense(
        db_session,
        report=report,
        category=ExpenseCategory.gift,
        amount="154.70",
        disclosure={"recipient": "Pat Client"},
    )
    gift_receipt = _processed_receipt(db_session, users_a["employee"], gift)
    r = _correct(employee_client, gift_receipt, "expense_type", "meals")
    assert r.status_code == 200 and r.json()["linked_expense_sync"] == "updated", r.text
    db_session.expire_all()
    fresh = db_session.get(Expense, gift.id)
    assert fresh is not None and fresh.category == ExpenseCategory.meals
    assert fresh.disclosure is None
    audit = db_session.scalars(
        sa.select(AuditLog)
        .where(AuditLog.entity_id == gift.id, AuditLog.action == "expense.updated")
        .order_by(AuditLog.created_at.desc())
    ).first()
    assert audit is not None and (audit.before_state or {}).get("disclosure") == {
        "recipient": "Pat Client"
    }
    assert (audit.after_state or {}).get("disclosure") is None


def test_an_unverified_attendee_count_never_auto_approves_a_team_meal(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    meal = f.make_expense(
        db_session, report=report, category=ExpenseCategory.meals, amount="480.00"
    )
    _processed_receipt(db_session, users_a["employee"], meal, amount=480.0)
    r = _patch(employee_client, meal.id, {"attendee_count": 8})  # Tier 3: $60 x 8 = $480
    assert r.status_code == 200, r.text
    [flag] = _flags(employee_client, report, "MEAL-05")
    assert flag["flag_type"] == "approval_required" and flag["requires_approver"] == "manager"
    assert flag["observed"]["could_not_verify"] == "attendees"
    db_session.refresh(report)
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )
    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)
    assert risk.requires_approver is True and decide_route(risk) == "escalate"


# ---------------------------------------------------------------------------
# Review fixes: no meal rule switched off by a typed attendee count; mileage is
# the claimant's word and always reaches a human
# ---------------------------------------------------------------------------


def test_a_team_meal_keeps_the_per_diem_when_meal05_is_archived(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    meal05 = db_session.scalars(
        sa.select(PolicyRule).where(
            PolicyRule.policy_id == policy.id, PolicyRule.source_clause_ref == "MEAL-05"
        )
    ).one()
    meal05.status = PolicyRuleStatus.archived
    db_session.flush()
    report = _report(db_session, users_a["employee"])
    meal = f.make_expense(
        db_session, report=report, category=ExpenseCategory.meals, amount="400.00"
    )
    r = _patch(employee_client, meal.id, {"attendee_count": 2})
    assert r.status_code == 200, r.text
    assert _flags(employee_client, report, "MEALS-PD"), "the per diem must still apply"
    assert not _flags(employee_client, report, "MEAL-05")
    db_session.refresh(report)
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )
    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)
    assert decide_route(risk) != "auto_approve"


def test_a_mileage_only_report_under_500_never_auto_approves(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    drive = {**TRIP, "distance": "290", "round_trip": True, "commute_offset": "0"}
    entry = _add(employee_client, report, drive).json()
    assert Decimal(entry["amount"]) == Decimal("406.00")  # 580 mi x $0.70
    flags = _all_flags(employee_client, report, entry["id"])
    assert not [fl for fl in flags if fl["severity"] == "blocking"], flags
    unverified = {
        fl["source_clause_ref"]: fl["observed"]["could_not_verify"]
        for fl in flags
        if fl["flag_type"] == "approval_required"
    }
    assert unverified == {"MIL-01": "distance", "MIL-02": "commute_offset"}
    db_session.refresh(report)
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )
    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)
    assert risk.requires_approver is True and decide_route(risk) == "escalate"


# ---------------------------------------------------------------------------
# Review fix: the approver sees the route it is asked to confirm
# ---------------------------------------------------------------------------


def test_a_trip_edit_keeps_the_route_visible_to_the_approver(
    policy: Policy,
    employee_client: TestClient,
    manager_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    report = _report(db_session, users_a["employee"])
    created = _add(employee_client, report, TRIP).json()
    assert created["description"] == "Boston office → Client site, Providence"
    trip = {k: TRIP[k] for k in ("distance", "unit", "origin", "round_trip", "commute_offset")}
    edited = {**trip, "destination": "New York", "distance": "215"}
    r = _patch(employee_client, created["id"], {"mileage": edited})
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["amount"]) == Decimal("287.00")  # (430 - 20) x $0.70
    # The generated description follows the trip — never the old route.
    assert body["description"] == "Boston office → New York"
    [rate_flag] = _flags(employee_client, report, "MIL-01")
    assert "Boston office → New York" in rate_flag["reason"]
    assert rate_flag["observed"]["destination"] == "New York"

    # What the approver reads: GET /approvals/{id} carries the stored trip.
    approval = f.make_approval(db_session, submitter=users_a["employee"], subject_id=report.id)
    db_session.commit()
    detail = manager_client.get(f"{API}/approvals/{approval.id}")
    assert detail.status_code == 200, detail.text
    [entry] = detail.json()["expense_report"]["entries"]
    assert entry["description"] == "Boston office → New York"
    seen = entry["mileage"]
    assert seen["origin"] == "Boston office" and seen["destination"] == "New York"
    assert seen["distance"] == "215" and seen["unit"] == "mi"
    assert seen["round_trip"] is True and seen["commute_offset"] == "20"
    assert seen["billable_miles"] == "410.0" and seen["rate_per_mile"] == "0.70"


def test_a_claimant_description_survives_a_trip_edit(
    policy: Policy, employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    created = _add(employee_client, report, TRIP).json()
    assert _patch(employee_client, created["id"], {"description": "Client QBR"}).status_code == 200
    edited = {**{k: TRIP[k] for k in TRIP if k != "expense_date"}, "destination": "New York"}
    body = _patch(employee_client, created["id"], {"mileage": edited}).json()
    assert body["description"] == "Client QBR"  # the claimant's own words are kept
    assert body["mileage"]["destination"] == "New York"
    # A description sent with the trip wins over regeneration.
    back = {**edited, "destination": "Client site, Providence"}
    both = _patch(employee_client, created["id"], {"mileage": back, "description": "Mine"})
    assert both.json()["description"] == "Mine"
