"""Phase 11 — database/seeds/cleanup_test_data.py removes ONLY acceptance-test data.

Each DB test builds, inside the rolled-back ``db_session`` transaction, a
miniature demo organisation: seeded demo users and a seeded report at the
baseline, then later test records (a "C7" report with an entry, receipt,
approval + step, matched card transaction, idempotency key and read-marker; an
"E2E" report; a QA user with its own report / trip / calendar event / card
transaction / conversation; an "E2E" trip with an approval; a "UAT" trip linked
from a kept report), plus records that must survive (ordinary reports, a
seeded-title report, a pre-baseline "E2E" report, a seeded receipt) and a second
organisation with look-alike test data.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from database.models.approval import Approval, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
from database.models.calendar_event import CalendarEvent
from database.models.card_transaction import CardTransaction
from database.models.conversation import Conversation, ConversationContextType
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.idempotency_key import IdempotencyKey
from database.models.identity import RoleKey, User, UserStatus
from database.models.notification_read import NotificationRead
from database.models.receipt import Receipt
from database.models.trip import Trip
from database.seeds import cleanup_test_data as ct

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=30)
ENABLED = {ct.ENABLE_ENV: "true"}


# ---------------------------------------------------------------------------
# Hermetic: matching rules and the seed mirror
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["C7", "c7 retry", "C7R round 2", "E2E flow report", "e2e-123", "QA run 4", "QA", "UAT",
     "UAT2 smoke", "Playwright trip", "C7R2611 Report A (to approve)",
     "C7MB Report 1 (manager browser)", "C7F1731 Assembled (agent)", "C7-05 verify hotel"],
)  # fmt: skip
def test_test_titles_match(title: str) -> None:
    assert ct.is_test_title(title)


@pytest.mark.parametrize(
    "title",
    ["Qatar trip", "Quarterly review", "Q2 Summit Expenses", "C70 hardware", "UATX", "", None,
     "Client visit (E2E later)", "C7000 budget", "Conference Expenses (Rejected)"],
)  # fmt: skip
def test_ordinary_titles_do_not_match(title: str | None) -> None:
    assert not ct.is_test_title(title)


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("qa.tester@atlas-demo.internal", True),
        ("qa-2@atlas-demo.internal", True),
        ("e2e-runner@atlas-demo.internal", True),
        ("playwright.bot@x.test", True),
        ("employee@atlas-demo.internal", False),
        ("manager2@atlas-demo.internal", False),
        ("qasim@atlas-demo.internal", False),
        ("quality@atlas-demo.internal", False),
    ],
)
def test_test_emails(email: str, expected: bool) -> None:
    assert ct.is_test_email(email) is expected


def test_seed_mirror_matches_the_seed_sources() -> None:
    """The protected baseline constants are exactly what the seeds create."""
    seed_all = (REPO_ROOT / "database" / "seeds" / "seed_all.py").read_text(encoding="utf-8")
    identity = (REPO_ROOT / "database" / "seeds" / "identity_seed.py").read_text(encoding="utf-8")
    titles = set(re.findall(r'^\s+title="([^"]+)",$', seed_all, re.MULTILINE))
    assert titles == set(ct.SEED_REPORT_TITLES)
    purposes = set(re.findall(r'^\s+purpose="([^"]+)",$', seed_all, re.MULTILINE))
    assert purposes == set(ct.SEED_TRIP_PURPOSES)
    emails = set(re.findall(r'email="([a-z0-9]+@atlas-demo\.internal)"', seed_all + identity))
    assert emails == set(ct.SEED_USER_EMAILS)
    assert all(not ct.is_test_title(t) for t in ct.SEED_REPORT_TITLES | ct.SEED_TRIP_PURPOSES)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


def _factory(db: Session) -> Any:
    return lambda: contextlib.nullcontext(db)


def _report(db: Session, owner: User, title: str, at: datetime, **kw: Any) -> ExpenseReport:
    return f.make_expense_report(db, owner=owner, title=title, created_at=at, **kw)


def _build(db: Session, org_name: str) -> dict[str, Any]:
    org = f.make_org(db, name=org_name)
    employee = f.make_user(db, org=org, email="employee@atlas-demo.internal", created_at=T0)
    manager = f.make_user(
        db, org=org, email="manager@atlas-demo.internal", role=RoleKey.manager, created_at=T0
    )
    qa = f.make_user(db, org=org, email="qa.tester@atlas-demo.internal", created_at=T1)
    real_user = f.make_user(db, org=org, email="jane@atlas-demo.internal", created_at=T1)

    seeded = _report(db, employee, "Q2 Summit Expenses", T0 + timedelta(minutes=5))
    seeded_receipt = f.make_receipt(
        db, uploaded_by=employee, image_url="receipts/seed/employee-meal-001.png", created_at=T0
    )

    c7 = _report(db, manager, "C7", T1)
    c7_entry = f.make_expense(db, report=c7, amount="999.00", created_at=T1)
    c7_receipt = f.make_receipt(
        db, uploaded_by=manager, image_url="receipts/uploads/c7.png", expense_id=c7_entry.id
    )
    seeded_receipt.expense_id = c7_entry.id  # a test attached the seeded receipt
    c7_card = f.make_card_transaction(db, user=employee, expense_id=c7_entry.id, matched=True)
    c7_approval = f.make_approval(
        db, submitter=manager, subject_type=ApprovalSubjectType.expense_report, subject_id=c7.id
    )
    f.make_approval_step(db, approval=c7_approval, approver_id=employee.id)
    db.add(NotificationRead(user_id=employee.id, notification_key=f"pending:{c7_approval.id}:1"))
    db.add(
        IdempotencyKey(
            organization_id=org.id,
            scope="expense_report.create",
            idempotency_key=f"k-{uuid.uuid4()}",
            entity_id=c7.id,
        )
    )

    e2e = _report(db, employee, "E2E flow report", T1)
    f.make_expense(db, report=e2e, created_at=T1)
    qa_owned = _report(db, qa, "Q2 Summit Expenses", T1)  # seeded title, test owner
    qa_trip = f.make_trip(db, traveler=qa, purpose="Client meeting", created_at=T1)
    qa_receipt = f.make_receipt(db, uploaded_by=qa, image_url="receipts/uploads/qa.png")
    qa_card = f.make_card_transaction(db, user=qa)
    db.add(CalendarEvent(organization_id=org.id, user_id=qa.id, title="QA sync", start_datetime=T1))
    db.add(
        Conversation(
            organization_id=org.id, user_id=qa.id, context_type=ConversationContextType.general
        )
    )

    e2e_trip = f.make_trip(db, traveler=employee, purpose="E2E trip", created_at=T1)
    e2e_trip_approval = f.make_approval(
        db, submitter=employee, subject_type=ApprovalSubjectType.trip, subject_id=e2e_trip.id
    )
    uat_trip = f.make_trip(db, traveler=employee, purpose="UAT trip", created_at=T1)
    kept = _report(db, employee, "Quarterly review", T1, trip_id=uat_trip.id)
    kept_entry = f.make_expense(db, report=kept, created_at=T1)
    qatar = _report(db, real_user, "Qatar trip", T1)
    old_e2e = _report(db, employee, "E2E before the seed", T0 - timedelta(days=1))
    seeded_trip = f.make_trip(
        db, traveler=employee, purpose="Q2 Engineering Summit", start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 14), created_at=T0 + timedelta(minutes=5),
    )  # fmt: skip
    db.flush()
    return {
        "org": org,
        "employee": employee,
        "manager": manager,
        "qa": qa,
        "real_user": real_user,
        "seeded": seeded,
        "seeded_receipt": seeded_receipt,
        "seeded_trip": seeded_trip,
        "c7": c7,
        "c7_entry": c7_entry,
        "c7_receipt": c7_receipt,
        "c7_card": c7_card,
        "c7_approval": c7_approval,
        "e2e": e2e,
        "qa_owned": qa_owned,
        "qa_trip": qa_trip,
        "qa_receipt": qa_receipt,
        "qa_card": qa_card,
        "e2e_trip": e2e_trip,
        "e2e_trip_approval": e2e_trip_approval,
        "uat_trip": uat_trip,
        "kept": kept,
        "kept_entry": kept_entry,
        "qatar": qatar,
        "old_e2e": old_e2e,
    }


@pytest.fixture()
def world(db_session: Session) -> dict[str, Any]:
    name = f"Atlas Demo Corp (cleanup test {uuid.uuid4().hex[:8]})"
    w = _build(db_session, name)
    other_org = f.make_org(db_session, name=f"Other Corp {uuid.uuid4().hex[:8]}")
    other_emp = f.make_user(
        db_session, org=other_org, email="employee@atlas-demo.internal", created_at=T0
    )
    other_qa = f.make_user(
        db_session, org=other_org, email="qa.tester@atlas-demo.internal", created_at=T1
    )
    w["other_org"] = other_org
    w["other_report"] = _report(db_session, other_emp, "E2E other org", T1)
    w["other_qa"] = other_qa
    # Commit into the outer (rolled-back) transaction so the tool's per-group
    # commits / rollbacks never discard the fixture.
    db_session.commit()
    # Tests work with ids: ORM instances of deleted rows cannot be refreshed.
    ids: dict[str, Any] = {k: v.id for k, v in w.items()}
    ids.update(name=name, qa_name=w["qa"].name, employee_name=w["employee"].name)
    return ids


def _exists(db: Session, model: Any, ident: uuid.UUID) -> bool:
    db.expire_all()
    return db.get(model, ident) is not None


def _snapshot(db: Session) -> dict[str, int]:
    tables = [
        "expense_reports", "expenses", "receipts", "approvals", "approval_steps", "trips",
        "card_transactions", "calendar_events", "conversations", "notification_reads",
        "idempotency_keys", "audit_log",
    ]  # fmt: skip
    counts = {t: db.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}
    counts["inactive_users"] = db.execute(
        sa.text("SELECT count(*) FROM users WHERE status = 'inactive'")
    ).scalar_one()
    return counts


def _run(db: Session, *args: str, env: dict[str, str] | None = None, deleted: list[str] | None
         = None) -> tuple[int, str]:  # fmt: skip
    lines: list[str] = []
    sink = deleted if deleted is not None else []
    code = ct.run(
        list(args),
        session_factory=_factory(db),
        env=env if env is not None else {},
        delete_object=sink.append,
        out=lines.append,
    )
    return code, "\n".join(lines)


# ---------------------------------------------------------------------------
# DB tests
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_dry_run_lists_and_writes_nothing(db_session: Session, world: dict[str, Any]) -> None:
    before = _snapshot(db_session)
    code, out = _run(db_session, "--org", world["name"], env=ENABLED)
    assert code == 0
    assert _snapshot(db_session) == before
    assert "DRY-RUN" in out and "nothing written" in out
    for key in ("c7", "e2e", "qa_owned", "e2e_trip", "qa_trip"):
        assert str(world[key]) in out
    assert f"user {world['qa']}" in out
    assert "'C7'" in out and "'E2E flow report'" in out
    # Skips are explained; kept records are not listed for deletion.
    assert f"skip trip {world['uat_trip']} — linked from a kept report" in out
    assert f"skip report {world['old_e2e']} — created before the seed baseline" in out
    for key in ("kept", "qatar", "seeded", "seeded_trip"):
        assert f" {world[key]} " not in out.replace(f"skip report {world[key]}", "")
    # No PII: no emails or user names.
    assert "@" not in out
    assert world["qa_name"] not in out and world["employee_name"] not in out


@pytest.mark.db
def test_apply_refuses_without_env_confirmation(db_session: Session, world: dict[str, Any]) -> None:
    before = _snapshot(db_session)
    for env in ({}, {ct.ENABLE_ENV: "false"}, {ct.ENABLE_ENV: "1"}):
        code, out = _run(db_session, "--org", world["name"], "--apply", env=env)
        assert code == 2 and "REFUSED" in out
    assert _snapshot(db_session) == before


def test_org_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        ct.run([], session_factory=lambda: contextlib.nullcontext(None), env=ENABLED, out=print)  # type: ignore[arg-type,return-value]
    assert exc.value.code == 2


@pytest.mark.db
def test_refuses_unknown_ambiguous_or_unseeded_org(
    db_session: Session, world: dict[str, Any]
) -> None:
    code, out = _run(db_session, "--org", "No Such Org", "--apply", env=ENABLED)
    assert code == 2 and "exactly one" in out
    f.make_org(db_session, name=world["name"])  # now ambiguous
    db_session.commit()
    code, out = _run(db_session, "--org", world["name"], "--apply", env=ENABLED)
    assert code == 2 and "exactly one" in out
    bare = f.make_org(db_session, name=f"Bare {uuid.uuid4().hex[:6]}")
    f.make_user(db_session, org=bare, email="qa.x@atlas-demo.internal")
    f.make_expense_report(
        db_session,
        owner=db_session.scalars(sa.select(User).where(User.organization_id == bare.id)).one(),
        title="E2E x",
    )
    db_session.commit()
    before = _snapshot(db_session)
    code, out = _run(db_session, "--org", bare.name, "--apply", env=ENABLED)
    assert code == 2 and "baseline" in out
    assert _snapshot(db_session) == before


@pytest.mark.db
def test_apply_removes_only_test_records(db_session: Session, world: dict[str, Any]) -> None:
    w = world
    deleted_keys: list[str] = []
    code, out = _run(db_session, "--org", w["name"], "--apply", env=ENABLED, deleted=deleted_keys)
    assert code == 0, out

    # Gone: test reports with their entries, receipts, approvals + steps, keys, markers.
    for key in ("c7", "e2e", "qa_owned"):
        assert not _exists(db_session, ExpenseReport, w[key]), key
    assert not _exists(db_session, Expense, w["c7_entry"])
    assert not _exists(db_session, Receipt, w["c7_receipt"])
    assert not _exists(db_session, Approval, w["c7_approval"])
    assert (
        db_session.scalars(
            sa.select(ApprovalStep).where(ApprovalStep.approval_id == w["c7_approval"])
        ).first()
        is None
    )
    assert (
        db_session.scalars(
            sa.select(IdempotencyKey).where(IdempotencyKey.entity_id == w["c7"])
        ).first()
        is None
    )
    assert (
        db_session.scalars(
            sa.select(NotificationRead).where(
                NotificationRead.notification_key == f"pending:{w['c7_approval']}:1"
            )
        ).first()
        is None
    )
    for key in ("e2e_trip", "qa_trip"):
        assert not _exists(db_session, Trip, w[key]), key
    assert not _exists(db_session, Approval, w["e2e_trip_approval"])

    # The QA account: records removed, account deactivated, never deleted.
    qa = db_session.get(User, w["qa"])
    assert qa is not None and qa.status == UserStatus.inactive
    assert not _exists(db_session, Receipt, w["qa_receipt"])
    assert not _exists(db_session, CardTransaction, w["qa_card"])
    org_id = w["org"]
    assert (
        db_session.scalars(sa.select(CalendarEvent).where(CalendarEvent.user_id == qa.id)).first()
        is None
    )
    assert (
        db_session.scalars(sa.select(Conversation).where(Conversation.user_id == qa.id)).first()
        is None
    )

    # Kept: baseline, ordinary data, linked trip, pre-baseline record, seed receipt.
    for key in ("seeded", "kept", "qatar", "old_e2e"):
        assert _exists(db_session, ExpenseReport, w[key]), key
    assert _exists(db_session, Expense, w["kept_entry"])
    for key in ("uat_trip", "seeded_trip"):
        assert _exists(db_session, Trip, w[key]), key
    seed_receipt = db_session.get(Receipt, w["seeded_receipt"])
    assert seed_receipt is not None and seed_receipt.expense_id is None
    card = db_session.get(CardTransaction, w["c7_card"])
    assert card is not None and card.expense_id is None  # demo feed row kept (FK SET NULL)
    for key in ("employee", "manager", "real_user"):
        u = db_session.get(User, w[key])
        assert u is not None and u.status == UserStatus.active, key

    # The other organisation is untouched.
    assert _exists(db_session, ExpenseReport, w["other_report"])
    other_qa = db_session.get(User, w["other_qa"])
    assert other_qa is not None and other_qa.status == UserStatus.active

    # Storage: only the non-seed objects, best effort.
    assert sorted(deleted_keys) == ["receipts/uploads/c7.png", "receipts/uploads/qa.png"]

    # One audit row with the counts and ids.
    (audit,) = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == ct.AUDIT_ACTION, AuditLog.organization_id == org_id
        )
    ).all()
    state = audit.after_state or {}
    assert audit.actor_id is None and audit.entity_id == org_id
    assert state["reports"] == 3 and state["trips"] == 2 and state["users_deactivated"] == 1
    assert state["approvals"] == 2 and state["receipts"] == 2 and state["entries"] == 2
    assert str(w["c7"]) in state["report_ids"]
    # Audit rows are never deleted.
    assert "applied:" in out


@pytest.mark.db
def test_second_apply_is_a_no_op(db_session: Session, world: dict[str, Any]) -> None:
    code, _ = _run(db_session, "--org", world["name"], "--apply", env=ENABLED)
    assert code == 0
    before = _snapshot(db_session)
    code, out = _run(db_session, "--org", world["name"], "--apply", env=ENABLED)
    assert code == 0
    assert "plan: 0 report(s), 0 trip(s), 0 user(s)" in out
    after = _snapshot(db_session)
    assert after.pop("audit_log") == before.pop("audit_log") + 1  # just the run's audit row
    assert after == before


@pytest.mark.db
def test_ids_file_is_still_protected(
    db_session: Session, world: dict[str, Any], tmp_path: Path
) -> None:
    w = world
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "# owner-confirmed leftovers\n"
        f"report:{w['qatar']}\n"
        f"report:{w['seeded']}\n"
        f"user:{w['real_user']}\n"
    )
    code, out = _run(db_session, "--org", w["name"], "--ids", str(ids), "--apply", env=ENABLED)
    assert code == 0, out
    assert not _exists(db_session, ExpenseReport, w["qatar"])  # explicitly listed
    assert _exists(db_session, ExpenseReport, w["seeded"])  # seeded: protected
    real = db_session.get(User, w["real_user"])
    assert real is not None and real.status == UserStatus.active
    assert f"skip user {w['real_user']} — not a test-pattern account" in out


@pytest.mark.db
def test_bad_ids_file_is_refused(
    db_session: Session, world: dict[str, Any], tmp_path: Path
) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text("expense:123\n")
    code, out = _run(db_session, "--org", world["name"], "--ids", str(ids), env=ENABLED)
    assert code == 2 and "REFUSED" in out


@pytest.mark.db
def test_a_failing_group_rolls_back_alone(
    db_session: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("simulated")

    monkeypatch.setattr(ct, "_delete_trip", boom)
    code, out = _run(db_session, "--org", world["name"], "--apply", env=ENABLED)
    assert code == 1
    assert f"FAILED group trip:{world['e2e_trip']}" in out
    assert _exists(db_session, Trip, world["e2e_trip"])
    assert not _exists(db_session, ExpenseReport, world["c7"])


@pytest.mark.db
def test_storage_failures_do_not_fail_the_run(db_session: Session, world: dict[str, Any]) -> None:
    def broken(_key: str) -> None:
        raise OSError("bucket unreachable")

    lines: list[str] = []
    code = ct.run(
        ["--org", world["name"], "--apply"],
        session_factory=_factory(db_session),
        env=ENABLED,
        delete_object=broken,
        out=lines.append,
    )
    assert code == 0
    assert "'storage_failed': 2" in "\n".join(lines)
    assert not _exists(db_session, ExpenseReport, world["c7"])


@pytest.mark.db
def test_a_rolled_back_group_keeps_its_storage_objects(
    db_session: Session, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ct._delete_report
    c7 = world["c7"]

    def fail_after_work(session: Session, **kw: Any) -> bool:
        done = original(session, **kw)
        if kw["ident"] == c7:
            raise RuntimeError("simulated failure after the deletes")
        return done

    monkeypatch.setattr(ct, "_delete_report", fail_after_work)
    deleted: list[str] = []
    code, out = _run(db_session, "--org", world["name"], "--apply", env=ENABLED, deleted=deleted)
    assert code == 1 and f"FAILED group report:{c7}" in out
    assert _exists(db_session, ExpenseReport, c7)
    assert _exists(db_session, Receipt, world["c7_receipt"])
    assert "receipts/uploads/c7.png" not in deleted
    assert "receipts/uploads/qa.png" in deleted
