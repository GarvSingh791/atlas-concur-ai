"""Follow-up (grades): AIR-07 / LOD-05 / NR-01 end to end on Postgres.

* pre-submit flags decided from the OWNER's grade of record (never the requester's);
* trusted evidence = the owner's receipt OCR / card merchant (another user's receipt
  is not evidence); the claimant's words only widen;
* an archived rule is not checked; approval-time routing (escalate vs auto-approve);
* migration 0025's data step on a populated policy: inserts only missing rules with
  audit rows, never touches an admin-edited rule, symmetric downgrade.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents import process_checks
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import PreSubmitFlag, run_presubmit
from ai_agents.context import AgentContext
from backend.tests.test_pg2_process_rules_db import _seed
from database.models.approval import ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import ExpenseCategory
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import ENTRY_RULES, RULES

pytestmark = pytest.mark.db

TODAY = date(2026, 11, 14)
_MIG = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "0025_grade_entitlement_rules.py"
)
GRADE_REFS = ("AIR-07", "LOD-05", "NR-01")


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


def _mig() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0025_db", _MIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id, user_id=user.id, granted_permission_keys=frozenset()
    )


def _entry(category: str, amount: float, **kw: Any) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency=kw.pop("currency", "USD"),
        expense_date=TODAY.isoformat(),
        vendor=kw.pop("vendor", None),
        description=kw.pop("description", None),
        status="matched",
        match_source=None,
        trip_segment_id=kw.pop("trip_segment_id", None),
        is_itemized=False,
        primary_receipt_id=kw.pop("primary_receipt_id", None),
        source_card_txn_id=kw.pop("source_card_txn_id", None),
        sibling_group_id=None,
        **kw,
    )


def _grade_flags(
    db: Session, owner: User, *entries: DraftEntry, requester: User | None = None
) -> list[PreSubmitFlag]:
    report = DraftReport(
        entries=list(entries),
        total_amount=sum(e.amount for e in entries),
        currency="USD",
        trip_id=None,
        title="t",
    )
    flags, _ = run_presubmit(
        report, context=_ctx(requester or owner), session=db, owner_id=owner.id
    )
    return [fl for fl in flags if fl.rule_type == "grade_entitlement"]


def _receipt(db: Session, owner: User, vendor: str, amount: float, **fields: Any) -> str:
    r = f.make_receipt(
        db,
        uploaded_by=owner,
        extracted_fields={"vendor": vendor, "amount": amount, **fields},
        field_provenance={"vendor": "deterministic", "amount": "deterministic"},
    )
    return str(r.id)


@pytest.fixture()
def org(db_session: Session) -> Organization:
    org = f.make_org(db_session)
    _seed(db_session, org)
    return org


def _user(db: Session, org: Organization, grade: str | None, **kw: Any) -> User:
    return f.make_user(db, org=org, grade=grade, **kw)


# ---------------------------------------------------------------------------
# Pre-submit
# ---------------------------------------------------------------------------


def test_svp_lounge_fee_is_in_policy_with_a_note(db_session: Session, org: Organization) -> None:
    svp = _user(db_session, org, "SVP")
    rid = _receipt(db_session, svp, "DELTA SKY CLUB ATL", 59.0)
    flags = _grade_flags(db_session, svp, _entry("airfare", 59.0, primary_receipt_id=rid))
    assert len(flags) == 1
    fl = flags[0]
    assert fl.flag_type == "policy_advisory" and fl.severity == "advisory"
    assert fl.source_clause_ref == "AIR-07" and fl.requires_approver is None
    assert fl.observed["traveller_grade"] == "SVP"


def test_ic_suite_is_a_violation_for_an_svp_approver(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    rid = _receipt(db_session, ic, "Hilton Chicago", 520.0)
    receipt_items = [{"description": "Junior Suite", "amount": 520.0}]
    db_session.execute(
        sa.text("UPDATE receipts SET extracted_fields = CAST(:f AS jsonb) WHERE id = :id"),
        {
            "f": json.dumps(
                {"vendor": "Hilton Chicago", "amount": 520.0, "line_items": receipt_items}
            ),
            "id": rid,
        },
    )
    db_session.expire_all()
    flags = _grade_flags(
        db_session, ic, _entry("lodging", 520.0, vendor="Hilton", primary_receipt_id=rid)
    )
    assert [fl.source_clause_ref for fl in flags] == ["LOD-05"]
    fl = flags[0]
    assert fl.flag_type == "policy_violation" and fl.severity == "blocking"
    assert fl.requires_approver == "SVP" and fl.justifiable is True
    # a written justification lets it be submitted; the violation stays for the approver
    justified = _grade_flags(
        db_session,
        ic,
        _entry("lodging", 520.0, primary_receipt_id=rid, justification="Only room left"),
    )
    assert justified[0].severity == "advisory" and justified[0].flag_type == "policy_violation"


def test_unknown_grade_needs_an_svp_level_approver(db_session: Session, org: Organization) -> None:
    nobody = _user(db_session, org, None)
    flags = _grade_flags(
        db_session, nobody, _entry("other", 450.0, vendor="Priority Pass membership")
    )
    assert [fl.source_clause_ref for fl in flags] == ["NR-01"]
    assert flags[0].flag_type == "approval_required" and flags[0].requires_approver == "SVP"
    assert flags[0].observed["could_not_verify"] == "traveller_grade"


def test_owner_grade_decides_never_the_requester(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    csuite = _user(db_session, org, "CSUITE", role=RoleKey.finance_admin)
    flags = _grade_flags(
        db_session, ic, _entry("airfare", 30.0, description="lounge day pass"), requester=csuite
    )
    assert flags[0].flag_type == "policy_violation"
    assert flags[0].observed["traveller_grade"] == "IC"


def test_another_users_receipt_is_not_evidence(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    other = _user(db_session, org, "IC")
    rid = _receipt(db_session, other, "UNITED CLUB ORD", 59.0)
    flags = _grade_flags(
        db_session, ic, _entry("airfare", 59.0, vendor="United", primary_receipt_id=rid)
    )
    assert flags == []


def test_card_merchant_is_trusted_evidence(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    txn = f.make_card_transaction(db_session, user=ic, amount="59.00", merchant="UNITED CLUB ORD")
    flags = _grade_flags(
        db_session,
        ic,
        _entry("airfare", 59.0, vendor="United Airlines", source_card_txn_id=str(txn.id)),
    )
    assert [fl.source_clause_ref for fl in flags] == ["AIR-07"]
    assert flags[0].observed["evidence"] == "receipt_or_card"


def test_another_users_card_transaction_is_not_evidence(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    other = _user(db_session, org, "IC")
    txn = f.make_card_transaction(
        db_session, user=other, amount="59.00", merchant="UNITED CLUB ORD"
    )
    flags = _grade_flags(
        db_session,
        ic,
        _entry("airfare", 59.0, vendor="United", source_card_txn_id=str(txn.id)),
    )
    assert flags == []


def _move_to_other_org(db: Session, table: str, row_id: Any) -> None:
    """Legacy / corrupt data: the row names the owner but sits in ANOTHER tenant."""
    foreign = f.make_org(db)
    db.execute(
        sa.text(f"UPDATE {table} SET organization_id = :o WHERE id = :id"),  # noqa: S608
        {"o": foreign.id, "id": row_id},
    )
    db.expire_all()


def test_card_transaction_from_another_org_is_not_evidence(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    txn = f.make_card_transaction(db_session, user=ic, amount="59.00", merchant="UNITED CLUB ORD")
    _move_to_other_org(db_session, "card_transactions", txn.id)
    flags = _grade_flags(
        db_session,
        ic,
        _entry("airfare", 59.0, vendor="United", source_card_txn_id=str(txn.id)),
    )
    assert flags == []


def test_receipt_from_another_org_is_not_evidence(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    rid = _receipt(db_session, ic, "UNITED CLUB ORD", 59.0)
    _move_to_other_org(db_session, "receipts", rid)
    flags = _grade_flags(
        db_session, ic, _entry("airfare", 59.0, vendor="United", primary_receipt_id=rid)
    )
    assert flags == []


def _folio(db: Session, owner: User, vendor: str, items: list[tuple[str, float]]) -> str:
    return _receipt(
        db,
        owner,
        vendor,
        sum(a for _, a in items),
        line_items=[{"description": d, "amount": a} for d, a in items],
    )


@pytest.mark.parametrize("category", ["lodging", "other", "meals"])
def test_recategorised_folio_still_raises_lod05(
    db_session: Session, org: Organization, category: str
) -> None:
    # Review fix: the claimant's category never narrows TRUSTED receipt evidence.
    ic = _user(db_session, org, "IC")
    rid = _folio(
        db_session,
        ic,
        "Marriott Marquis",
        [("Room Charge", 250.0), ("Junior Suite Supplement", 120.0)],
    )
    flags = _grade_flags(
        db_session, ic, _entry(category, 370.0, vendor="Misc", primary_receipt_id=rid)
    )
    assert [(fl.flag_type, fl.source_clause_ref) for fl in flags] == [
        ("policy_violation", "LOD-05")
    ]
    assert flags[0].observed["amount"] == "120.00"


def test_recategorised_upgrade_line_still_raises_lod05(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    rid = _folio(db_session, ic, "Hyatt Regency", [("Room Charge", 250.0), ("Upgrade", 80.0)])
    flags = _grade_flags(db_session, ic, _entry("other", 330.0, primary_receipt_id=rid))
    assert [fl.source_clause_ref for fl in flags] == ["LOD-05"]


@pytest.mark.parametrize(
    ("vendor", "items", "category"),
    [
        ("Adobe Inc", [("Adobe Creative Suite upgrade", 240.0)], "other"),
        ("Microsoft", [("Microsoft Office Suite upgrade", 99.0)], "meals"),
        ("Regus - Conference room", [("Room hire", 150.0), ("AV upgrade", 40.0)], "other"),
    ],
)
def test_software_or_office_receipt_raises_no_lod05_for_an_ic(
    db_session: Session,
    org: Organization,
    vendor: str,
    items: list[tuple[str, float]],
    category: str,
) -> None:
    # Review fix: "suite upgrade" / a conference "room" never puts a line at a hotel, so
    # an IC's software or office purchase is not routed to an SVP.
    ic = _user(db_session, org, "IC")
    rid = _folio(db_session, ic, vendor, items)
    total = sum(a for _, a in items)
    flags = _grade_flags(
        db_session,
        ic,
        _entry(category, total, vendor=vendor, description=items[0][0], primary_receipt_id=rid),
    )
    assert flags == []


def _segment(db: Session, traveler: User, segment_type: str) -> str:
    from database.models.trip_segment import SegmentType

    trip = f.make_trip(db, traveler=traveler)
    return str(f.make_trip_segment(db, trip=trip, segment_type=SegmentType(segment_type)).id)


def test_owned_hotel_segment_places_the_line_at_a_hotel(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    rid = _folio(db_session, ic, "Acme Billing", [("Upgrade", 80.0)])
    plain = _grade_flags(db_session, ic, _entry("other", 80.0, primary_receipt_id=rid))
    assert plain == []
    seg = _segment(db_session, ic, "hotel")
    flags = _grade_flags(
        db_session, ic, _entry("other", 80.0, primary_receipt_id=rid, trip_segment_id=seg)
    )
    assert [fl.source_clause_ref for fl in flags] == ["LOD-05"]


def test_foreign_or_other_users_segment_adds_no_context(
    db_session: Session, org: Organization
) -> None:
    ic = _user(db_session, org, "IC")
    colleague = _user(db_session, org, "IC")
    rid = _folio(db_session, ic, "Acme Billing", [("Upgrade", 80.0)])
    theirs = _segment(db_session, colleague, "hotel")
    assert (
        _grade_flags(
            db_session, ic, _entry("other", 80.0, primary_receipt_id=rid, trip_segment_id=theirs)
        )
        == []
    )
    mine_elsewhere = _segment(db_session, ic, "hotel")
    trip_id = db_session.execute(
        sa.text("SELECT trip_id FROM trip_segments WHERE id = :id"), {"id": mine_elsewhere}
    ).scalar_one()
    _move_to_other_org(db_session, "trips", trip_id)
    assert (
        _grade_flags(
            db_session,
            ic,
            _entry("other", 80.0, primary_receipt_id=rid, trip_segment_id=mine_elsewhere),
        )
        == []
    )


def test_typed_words_cannot_hide_a_receipt_match(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    rid = _receipt(db_session, ic, "DELTA SKY CLUB", 59.0)
    flags = _grade_flags(
        db_session,
        ic,
        _entry("airfare", 59.0, vendor="Delta", description="Seat", primary_receipt_id=rid),
    )
    assert flags and flags[0].flag_type == "policy_violation"


def test_archived_rule_is_not_checked(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    db_session.execute(
        sa.update(PolicyRule)
        .where(PolicyRule.source_clause_ref == "AIR-07")
        .values(status=PolicyRuleStatus.archived)
    )
    db_session.flush()
    assert _grade_flags(db_session, ic, _entry("airfare", 20.0, description="lounge pass")) == []


def test_eur_amount_converted(db_session: Session, org: Organization) -> None:
    ic = _user(db_session, org, "IC")
    flags = _grade_flags(
        db_session, ic, _entry("airfare", 40.0, currency="EUR", description="Priority boarding")
    )
    assert flags[0].observed["currency"] == "EUR" and flags[0].observed["amount_usd"] != "40.00"


# ---------------------------------------------------------------------------
# Approval-time routing
# ---------------------------------------------------------------------------


def _submitted_report(db: Session, owner: User, *, description: str, amount: str) -> Any:
    report = f.make_expense_report(
        db, owner=owner, status=ExpenseReportStatus.submitted, total_amount=amount
    )
    f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.other,
        amount=amount,
        expense_date=TODAY,
        vendor="Delta",
        description=description,
    )
    return report


@pytest.mark.parametrize(("grade", "route"), [("SVP", "auto_approve"), ("VP", "escalate")])
def test_small_lounge_fee_routes_by_grade(
    db_session: Session, org: Organization, grade: str, route: str
) -> None:
    owner = _user(db_session, org, grade)
    report = _submitted_report(db_session, owner, description="lounge day pass", amount="20.00")
    a = assess_risk(
        report, ApprovalSubjectType.expense_report, context=_ctx(owner), session=db_session
    )
    assert decide_route(a) == route
    assert any(fl.get("source_clause_ref") == "AIR-07" for fl in a.flags_snapshot)


# ---------------------------------------------------------------------------
# Migration 0025 (data step) on a populated policy
# ---------------------------------------------------------------------------


def _policy_without_grade_rules(db: Session, org: Organization) -> Any:
    policy = f.make_policy(db, org=org, is_active=True)
    for r in (*RULES, *ENTRY_RULES):
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


def _grade_rows(db: Session, policy: Any) -> list[PolicyRule]:
    return list(
        db.scalars(
            sa.select(PolicyRule).where(
                PolicyRule.policy_id == policy.id, PolicyRule.source_clause_ref.in_(GRADE_REFS)
            )
        )
    )


def test_migration_inserts_missing_rules_idempotently(db_session: Session) -> None:
    mig = _mig()
    org = f.make_org(db_session)
    policy = _policy_without_grade_rules(db_session, org)
    # an admin already authored (and archived) their own NR-01 rule: never touched
    admin = f.make_user(db_session, org=org, role=RoleKey.finance_admin)
    own = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.category_allow,
        applies_to=PolicyAppliesTo.all,
        definition={
            "kind": "grade_entitlement",
            "entitlement": "lounge_membership",
            "min_grade": "CSUITE",
        },
        source_clause_ref="NR-01",
        status=PolicyRuleStatus.archived,
    )
    db_session.add(
        AuditLog(
            organization_id=org.id,
            actor_id=admin.id,
            action="policy_rule.updated",
            entity_type="policy_rule",
            entity_id=own.id,
        )
    )
    db_session.flush()
    conn = db_session.connection()
    first = mig.apply_grade_rules(conn, forward=True)
    # other test orgs' policies may also be targets; ours got exactly the two missing ones
    rows = {r.source_clause_ref: r for r in _grade_rows(db_session, policy)}
    assert set(rows) == set(GRADE_REFS) and first["inserted"] >= 2
    assert rows["NR-01"].id == own.id and rows["NR-01"].definition["min_grade"] == "CSUITE"
    assert rows["AIR-07"].status == PolicyRuleStatus.active
    audits = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_id.in_([rows["AIR-07"].id, rows["LOD-05"].id]),
            AuditLog.action == "policy_rule.seeded",
        )
    ).all()
    assert len(audits) == 2 and all(a.actor_id is None for a in audits)
    again = mig.apply_grade_rules(conn, forward=True)
    assert again["inserted"] == 0
    # downgrade removes only the untouched literal rows; the admin's rule stays
    mig.apply_grade_rules(conn, forward=False)
    db_session.expire_all()
    left = _grade_rows(db_session, policy)
    assert [r.id for r in left] == [own.id]


def test_downgrade_keeps_every_rule_a_person_touched(db_session: Session) -> None:
    """Review fix: each downgrade guard is needed on its own.

    * AIR-07 still equals the literal, but an admin acted on it (audit row with an actor)
      -> kept (the actor guard alone saves it);
    * LOD-05 has an admin-changed definition and NO actor audit (edited outside the app)
      -> kept (the definition guard alone saves it);
    * NR-01 is untouched -> removed, with a system ``policy_rule.unseeded`` audit row.
    """
    mig = _mig()
    org = f.make_org(db_session)
    policy = _policy_without_grade_rules(db_session, org)
    admin = f.make_user(db_session, org=org, role=RoleKey.finance_admin)
    conn = db_session.connection()
    mig.apply_grade_rules(conn, forward=True)
    db_session.expire_all()
    rows = {r.source_clause_ref: r for r in _grade_rows(db_session, policy)}
    assert set(rows) == set(GRADE_REFS)
    air07, lod05 = rows["AIR-07"], rows["LOD-05"]
    nr01_id = rows["NR-01"].id
    db_session.add(
        AuditLog(
            organization_id=org.id,
            actor_id=admin.id,
            action="policy_rule.status_changed",
            entity_type="policy_rule",
            entity_id=air07.id,
        )
    )
    changed = {**lod05.definition, "min_grade": "VP"}
    db_session.execute(
        sa.text("UPDATE policy_rules SET definition = CAST(:d AS jsonb) WHERE id = :id"),
        {"d": json.dumps(changed), "id": lod05.id},
    )
    db_session.flush()
    assert not db_session.scalars(
        sa.select(AuditLog).where(AuditLog.entity_id == lod05.id, AuditLog.actor_id.isnot(None))
    ).all()

    mig.apply_grade_rules(conn, forward=False)
    db_session.expire_all()
    left = {r.source_clause_ref: r for r in _grade_rows(db_session, policy)}
    assert set(left) == {"AIR-07", "LOD-05"}
    assert left["AIR-07"].id == air07.id
    assert left["LOD-05"].id == lod05.id and left["LOD-05"].definition["min_grade"] == "VP"

    unseeded = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "policy_rule.unseeded",
            AuditLog.entity_id.in_([air07.id, lod05.id, nr01_id]),
        )
    ).all()
    assert [a.entity_id for a in unseeded] == [nr01_id]
    audit = unseeded[0]
    assert audit.actor_id is None and audit.organization_id == org.id
    assert audit.entity_type == "policy_rule"
    assert audit.before_state is not None
    assert audit.before_state["source_clause_ref"] == "NR-01"
    assert audit.before_state["status"] == "active"
    assert audit.after_state is not None and "downgrade" in audit.after_state["source"]
