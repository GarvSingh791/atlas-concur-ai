"""Remove acceptance-test records from the demo organisation (phase 11 ops tool).

Staging still holds records created by earlier acceptance rounds (C7 / C7R /
UAT / QA / E2E runs) next to the seeded demo data: reports titled "C7",
"E2E …", "QA run …", trips with test purposes, ``qa.*`` / ``e2e-*`` users.  This
tool finds them by CONSERVATIVE, documented rules and removes them.

Safety model
  * DRY-RUN is the default: it prints what it WOULD do (counts, ids and titles /
    purposes only — no emails, names or amounts) and writes nothing.
  * Writing needs BOTH ``--apply`` AND the env ``CLEANUP_TEST_DATA_ENABLED=true``.
  * ``--org`` is always required and must name exactly one organisation; every
    query is scoped to that organisation id.
  * The seed baseline must exist in that organisation (the seeded demo users,
    ``SEED_USER_EMAILS``), else the tool refuses: it never runs against an org it
    cannot tell apart from real data.

Matching rules (a record must match a rule AND pass every protection)
  1. Expense reports whose title, or trips whose purpose, starts with a test
     marker: ``E2E``, ``QA``, ``QA run``, ``C7``, ``C7R``, ``UAT``/``UAT2``…,
     ``Playwright`` (case-insensitive, whole word — ``Qatar`` does not match).
  2. Reports / trips owned by a TEST USER: an org user whose email local part
     starts with ``qa.``/``qa-``/``qa_``/``qa+``, ``e2e…`` or ``playwright…`` (never
     one of the seeded demo users).
  3. Ids listed in an ``--ids`` file (``report:<uuid>``, ``trip:<uuid>``,
     ``user:<uuid>``; ``#`` comments) — still subject to every protection.
Protections
  * created at or before the seed baseline (the earliest seeded demo user) — skip;
  * a seeded report title / trip purpose (``SEED_REPORT_TITLES`` /
    ``SEED_TRIP_PURPOSES``, mirrored from ``seed_all.py`` and pinned by a test)
    owned by a seeded demo user — skip;
  * a trip still linked from a report that is kept (by ``trip_id`` or an entry's
    segment) — skip, so no kept record is modified by an FK ``SET NULL``;
  * seeded receipts (``receipts/seed/…`` image keys) are never deleted;
  * USERS ARE NEVER DELETED: a test user is DEACTIVATED (status ``inactive``) after
    its records are removed.  Deleting a user would rewrite ``audit_log.actor_id``
    (FK ``SET NULL``) on the immutable audit trail.  Only test-pattern users are
    touched; a seeded demo user is never a test user.

What is deleted, per group, in FK-safe order, one transaction per group
  report  → notification read-markers, idempotency keys, approvals (+ steps by
            cascade), receipts attached to its entries (non-seed; storage
            objects deleted best-effort after commit), entries, the report.
            Card transactions matched to its entries keep the row (FK SET NULL).
  trip    → read-markers, idempotency keys, approvals (+ steps), the trip
            (segments by cascade).
  user    → its remaining non-seed receipts, card transactions, calendar events,
            conversations (+ messages by cascade), then status → inactive.
A group that fails rolls back alone and is reported; the others proceed.
Audit rows are never deleted (the compliance record is append-only).  One
``ops.test_data_cleanup`` audit row (actor = system) records the counts and ids.

Idempotent: a second run finds nothing (deactivated users are not re-counted).

Run (dry-run):  python -m database.seeds.cleanup_test_data --org "Atlas Demo Corp"
Apply:          CLEANUP_TEST_DATA_ENABLED=true \\
                python -m database.seeds.cleanup_test_data --org "Atlas Demo Corp" --apply
Runbook: docs/runbooks/staging-test-data-cleanup.md
Exit status: 0 ok, 1 some group failed, 2 refused (missing confirmation / org / baseline).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backend.app.core.audit import record_audit  # noqa: E402
from database.models.approval import Approval, ApprovalSubjectType  # noqa: E402
from database.models.calendar_event import CalendarEvent  # noqa: E402
from database.models.card_transaction import CardTransaction  # noqa: E402
from database.models.conversation import Conversation  # noqa: E402
from database.models.expense import Expense  # noqa: E402
from database.models.expense_report import ExpenseReport  # noqa: E402
from database.models.idempotency_key import IdempotencyKey  # noqa: E402
from database.models.identity import Organization, User, UserStatus  # noqa: E402
from database.models.notification_read import NotificationRead  # noqa: E402
from database.models.receipt import Receipt  # noqa: E402
from database.models.trip import Trip  # noqa: E402
from database.models.trip_segment import TripSegment  # noqa: E402

logger = logging.getLogger(__name__)

ENABLE_ENV = "CLEANUP_TEST_DATA_ENABLED"
AUDIT_ACTION = "ops.test_data_cleanup"

# ---- The seed baseline (mirrors database/seeds/seed_all.py + identity_seed.py;
# backend/tests/test_ps_ops_cleanup_db.py fails if they drift) ----------------
SEED_USER_EMAILS = frozenset(
    {
        "employee@atlas-demo.internal",
        "manager@atlas-demo.internal",
        "finance@atlas-demo.internal",
        "manager2@atlas-demo.internal",
        "employee2@atlas-demo.internal",
    }
)
SEED_REPORT_TITLES = frozenset(
    {
        "Q2 Summit Expenses",
        "June Miscellaneous",
        "Client Visit Expenses",
        "Conference Expenses (Rejected)",
        "Office Supplies",
        "Q3 Conference & Travel",
    }
)
SEED_TRIP_PURPOSES = frozenset({"Q2 Engineering Summit", "Client presentation — Chicago"})
SEED_RECEIPT_PREFIX = "receipts/seed/"

# ---- Test-record markers ------------------------------------------------------
TEST_TITLE = re.compile(
    r"^\s*(?:e2e|qa(?:\s+run)?|c7(?:[a-z]+\d*)?|uat\d*|playwright)\b", re.IGNORECASE
)
TEST_EMAIL_LOCAL = re.compile(r"^(?:qa[._+-]|e2e|playwright)", re.IGNORECASE)

_ID_KINDS = ("report", "trip", "user")


class Refused(Exception):
    """The run is not allowed (missing confirmation, org or seed baseline)."""


def is_test_title(text: str | None) -> bool:
    return bool(text) and bool(TEST_TITLE.match(text or ""))


def is_test_email(email: str) -> bool:
    local = email.split("@", 1)[0]
    return email.lower() not in SEED_USER_EMAILS and bool(TEST_EMAIL_LOCAL.match(local))


# ---------------------------------------------------------------------------
# Planning (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    id: uuid.UUID
    label: str  # title / purpose; "" for users (no PII)
    reason: str


@dataclass
class Plan:
    org_id: uuid.UUID
    baseline_at: datetime
    reports: list[Item] = field(default_factory=list)
    trips: list[Item] = field(default_factory=list)
    users: list[Item] = field(default_factory=list)
    skipped: list[tuple[str, uuid.UUID, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "reports": len(self.reports),
            "trips": len(self.trips),
            "users": len(self.users),
            "skipped": len(self.skipped),
        }


def read_ids_file(path: Path) -> dict[str, set[uuid.UUID]]:
    ids: dict[str, set[uuid.UUID]] = {k: set() for k in _ID_KINDS}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        kind, _, value = line.partition(":")
        kind = kind.strip().lower()
        if kind not in ids:
            raise Refused(f"--ids line {n}: expected report:/trip:/user:<uuid>")
        try:
            ids[kind].add(uuid.UUID(value.strip()))
        except ValueError as exc:
            raise Refused(f"--ids line {n}: not a uuid") from exc
    return ids


def resolve_org(session: Session, org_name: str) -> Organization:
    orgs = list(session.scalars(sa.select(Organization).where(Organization.name == org_name)))
    if len(orgs) != 1:
        raise Refused(f"--org must name exactly one organisation (found {len(orgs)})")
    return orgs[0]


def build_plan(
    session: Session,
    org: Organization,
    explicit: dict[str, set[uuid.UUID]] | None = None,
) -> Plan:
    explicit = explicit or {k: set() for k in _ID_KINDS}
    users = list(session.scalars(sa.select(User).where(User.organization_id == org.id)))
    seed_users = {u.id for u in users if u.email.lower() in SEED_USER_EMAILS}
    if not seed_users:
        raise Refused("no seeded demo users in this organisation: no baseline, refusing")
    baseline_at = min(u.created_at for u in users if u.id in seed_users)
    plan = Plan(org_id=org.id, baseline_at=baseline_at)

    test_users: set[uuid.UUID] = set()
    for u in sorted(users, key=lambda u: str(u.id)):
        wanted = is_test_email(u.email) or u.id in explicit["user"]
        if not wanted:
            continue
        if not is_test_email(u.email):
            plan.skipped.append(("user", u.id, "not a test-pattern account"))
        elif u.created_at <= baseline_at:
            plan.skipped.append(("user", u.id, "created before the seed baseline"))
        else:
            test_users.add(u.id)
            if u.status != UserStatus.inactive:
                plan.users.append(Item(u.id, "", "test account"))

    reports = session.scalars(
        sa.select(ExpenseReport)
        .where(ExpenseReport.organization_id == org.id)
        .order_by(ExpenseReport.created_at, ExpenseReport.id)
    )
    for r in reports:
        reason = _match_reason(r.title, r.owner_id, r.id, test_users, explicit["report"])
        if reason is None:
            continue
        skip = _before_baseline(r.created_at, baseline_at)
        if skip is None and r.owner_id in seed_users and r.title in SEED_REPORT_TITLES:
            skip = "seeded report"
        if skip:
            plan.skipped.append(("report", r.id, skip))
        else:
            plan.reports.append(Item(r.id, r.title, reason))

    doomed_reports = {i.id for i in plan.reports}
    trips = session.scalars(
        sa.select(Trip).where(Trip.organization_id == org.id).order_by(Trip.created_at, Trip.id)
    )
    for t in trips:
        reason = _match_reason(t.purpose, t.traveler_id, t.id, test_users, explicit["trip"])
        if reason is None:
            continue
        skip = _before_baseline(t.created_at, baseline_at)
        if skip is None and t.traveler_id in seed_users and t.purpose in SEED_TRIP_PURPOSES:
            skip = "seeded trip"
        if skip is None and _trip_linked_to_kept_report(session, t.id, doomed_reports):
            skip = "linked from a kept report"
        if skip:
            plan.skipped.append(("trip", t.id, skip))
        else:
            plan.trips.append(Item(t.id, t.purpose or "", reason))
    return plan


def _match_reason(
    text: str | None,
    owner_id: uuid.UUID,
    record_id: uuid.UUID,
    test_users: set[uuid.UUID],
    explicit: set[uuid.UUID],
) -> str | None:
    if is_test_title(text):
        return "test title"
    if owner_id in test_users:
        return "owned by a test account"
    if record_id in explicit:
        return "listed in --ids"
    return None


def _before_baseline(created_at: datetime, baseline_at: datetime) -> str | None:
    if created_at <= baseline_at:
        return "created before the seed baseline"
    return None


def _trip_linked_to_kept_report(
    session: Session, trip_id: uuid.UUID, doomed_reports: set[uuid.UUID]
) -> bool:
    by_trip = sa.select(ExpenseReport.id).where(ExpenseReport.trip_id == trip_id)
    by_segment = (
        sa.select(Expense.report_id)
        .join(TripSegment, TripSegment.id == Expense.trip_segment_id)
        .where(TripSegment.trip_id == trip_id)
    )
    linked = set(session.scalars(by_trip)) | set(session.scalars(by_segment))
    return bool(linked - doomed_reports)


# ---------------------------------------------------------------------------
# Applying (one transaction per group)
# ---------------------------------------------------------------------------


@dataclass
class Result:
    deleted_reports: list[str] = field(default_factory=list)
    deleted_trips: list[str] = field(default_factory=list)
    deactivated_users: list[str] = field(default_factory=list)
    deleted_entries: int = 0
    deleted_receipts: int = 0
    deleted_approvals: int = 0
    failed: list[str] = field(default_factory=list)
    storage_keys: list[str] = field(default_factory=list)
    storage_failed: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "reports": len(self.deleted_reports),
            "trips": len(self.deleted_trips),
            "users_deactivated": len(self.deactivated_users),
            "entries": self.deleted_entries,
            "receipts": self.deleted_receipts,
            "approvals": self.deleted_approvals,
            "storage_objects": len(self.storage_keys) - self.storage_failed,
            "storage_failed": self.storage_failed,
            "failed_groups": len(self.failed),
        }


def _delete_markers_and_keys(session: Session, ids: Iterable[uuid.UUID]) -> None:
    id_list = list(ids)
    if not id_list:
        return
    session.execute(
        sa.delete(NotificationRead).where(
            sa.func.split_part(NotificationRead.notification_key, ":", 2).in_(
                [str(i) for i in id_list]
            )
        )
    )
    session.execute(sa.delete(IdempotencyKey).where(IdempotencyKey.entity_id.in_(id_list)))


def _delete_approvals(
    session: Session, org_id: uuid.UUID, kind: ApprovalSubjectType, subject: uuid.UUID
) -> list[uuid.UUID]:
    approval_ids = list(
        session.scalars(
            sa.select(Approval.id).where(
                Approval.organization_id == org_id,
                Approval.subject_type == kind,
                Approval.subject_id == subject,
            )
        )
    )
    if approval_ids:
        _delete_markers_and_keys(session, approval_ids)
        session.execute(sa.delete(Approval).where(Approval.id.in_(approval_ids)))
    return approval_ids


def _delete_receipts(session: Session, where: Any, result: Result) -> None:
    rows = session.execute(
        sa.select(Receipt.id, Receipt.image_url).where(
            where, ~Receipt.image_url.startswith(SEED_RECEIPT_PREFIX)
        )
    ).all()
    if not rows:
        return
    session.execute(sa.delete(Receipt).where(Receipt.id.in_([r.id for r in rows])))
    result.deleted_receipts += len(rows)
    result.storage_keys.extend(r.image_url for r in rows if r.image_url)


def _delete_report(session: Session, *, org_id: uuid.UUID, ident: uuid.UUID, r: Result) -> bool:
    report_id = ident
    report = session.scalars(
        sa.select(ExpenseReport)
        .where(ExpenseReport.id == report_id, ExpenseReport.organization_id == org_id)
        .with_for_update()
    ).first()
    if report is None:
        return False  # already gone (idempotent)
    entry_ids = list(session.scalars(sa.select(Expense.id).where(Expense.report_id == report_id)))
    _delete_markers_and_keys(session, [report_id, *entry_ids])
    r.deleted_approvals += len(
        _delete_approvals(session, org_id, ApprovalSubjectType.expense_report, report_id)
    )
    if entry_ids:
        _delete_receipts(session, Receipt.expense_id.in_(entry_ids), r)
        session.execute(sa.delete(Expense).where(Expense.id.in_(entry_ids)))
    session.execute(sa.delete(ExpenseReport).where(ExpenseReport.id == report_id))
    r.deleted_entries += len(entry_ids)
    return True


def _delete_trip(session: Session, *, org_id: uuid.UUID, ident: uuid.UUID, r: Result) -> bool:
    trip_id = ident
    trip = session.scalars(
        sa.select(Trip).where(Trip.id == trip_id, Trip.organization_id == org_id).with_for_update()
    ).first()
    if trip is None:
        return False
    _delete_markers_and_keys(session, [trip_id])
    r.deleted_approvals += len(
        _delete_approvals(session, org_id, ApprovalSubjectType.trip, trip_id)
    )
    session.execute(sa.delete(Trip).where(Trip.id == trip_id))
    return True


def _retire_user(session: Session, *, org_id: uuid.UUID, ident: uuid.UUID, r: Result) -> bool:
    user_id = ident
    user = session.scalars(
        sa.select(User).where(User.id == user_id, User.organization_id == org_id).with_for_update()
    ).first()
    if user is None or not is_test_email(user.email):
        return False
    _delete_receipts(session, Receipt.uploaded_by == user_id, r)
    session.execute(sa.delete(CardTransaction).where(CardTransaction.user_id == user_id))
    session.execute(sa.delete(CalendarEvent).where(CalendarEvent.user_id == user_id))
    session.execute(sa.delete(Conversation).where(Conversation.user_id == user_id))
    user.status = UserStatus.inactive
    return True


SessionFactory = Callable[[], AbstractContextManager[Session]]
GroupFn = Callable[..., bool]


def _run_group(factory: SessionFactory, label: str, fn: Callable[[Session], bool]) -> bool | None:
    """One transaction per group; returns True/False from fn, None on failure."""
    try:
        with factory() as session:
            try:
                done = fn(session)
                session.commit()
                return done
            except Exception:
                session.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 — one bad group must not stop the rest
        logger.error("cleanup group %s failed: %s", label, exc.__class__.__name__)
        return None


def apply_plan(
    factory: SessionFactory,
    plan: Plan,
    *,
    delete_object: Callable[[str], None] | None = None,
) -> Result:
    result = Result()
    org_id = plan.org_id
    groups: list[tuple[str, list[Item], GroupFn, list[str]]] = [
        ("report", plan.reports, _delete_report, result.deleted_reports),
        ("trip", plan.trips, _delete_trip, result.deleted_trips),
        ("user", plan.users, _retire_user, result.deactivated_users),
    ]
    for kind, items, fn, done_ids in groups:
        for item in items:
            # Counters and storage keys are collected per group and merged only
            # after its commit: a rolled-back group must not delete storage objects.
            acc = Result()
            done = _run_group(
                factory, f"{kind} {item.id}", partial(fn, org_id=org_id, ident=item.id, r=acc)
            )
            if done is None:
                result.failed.append(f"{kind}:{item.id}")
                continue
            if done:
                done_ids.append(str(item.id))
            result.deleted_entries += acc.deleted_entries
            result.deleted_receipts += acc.deleted_receipts
            result.deleted_approvals += acc.deleted_approvals
            result.storage_keys.extend(acc.storage_keys)

    # Storage objects: best effort, after the rows are gone.
    if delete_object is not None:
        for key in result.storage_keys:
            try:
                delete_object(key)
            except Exception:  # noqa: BLE001
                result.storage_failed += 1

    def _audit(session: Session) -> bool:
        record_audit(
            session,
            organization_id=org_id,
            action=AUDIT_ACTION,
            entity_type="organization",
            entity_id=org_id,
            actor_id=None,
            after_state={
                **result.summary(),
                "skipped": len(plan.skipped),
                "report_ids": result.deleted_reports,
                "trip_ids": result.deleted_trips,
                "user_ids": result.deactivated_users,
                "failed": result.failed,
            },
        )
        return True

    if _run_group(factory, "audit", _audit) is None:
        result.failed.append("audit")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_plan(plan: Plan, out: Callable[[str], None]) -> None:
    out(f"seed baseline: {plan.baseline_at.isoformat()}")
    for kind, items in (("report", plan.reports), ("trip", plan.trips), ("user", plan.users)):
        for it in items:
            label = f" {it.label!r}" if it.label else ""
            out(f"  {kind} {it.id}{label} — {it.reason}")
    for kind, rid, why in plan.skipped:
        out(f"  skip {kind} {rid} — {why}")
    c = plan.counts()
    out(
        f"plan: {c['reports']} report(s), {c['trips']} trip(s), "
        f"{c['users']} user(s) to deactivate, {c['skipped']} skipped"
    )


def run(
    argv: list[str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    env: dict[str, str] | None = None,
    delete_object: Callable[[str], None] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(
        description="Remove acceptance-test records (dry-run default)."
    )
    parser.add_argument(
        "--org", required=True, help='exact organisation name, e.g. "Atlas Demo Corp"'
    )
    parser.add_argument("--apply", action="store_true", help="write (needs the env confirmation)")
    parser.add_argument("--ids", type=Path, help="file of report:/trip:/user:<uuid> lines")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    if args.apply and env.get(ENABLE_ENV, "").strip().lower() != "true":
        out(f"REFUSED: --apply also needs {ENABLE_ENV}=true in the environment")
        return 2
    if session_factory is None:
        from backend.app.core.database import get_sessionmaker

        session_factory = get_sessionmaker()
    try:
        explicit = read_ids_file(args.ids) if args.ids else None
        with session_factory() as session:
            org = resolve_org(session, args.org)
            plan = build_plan(session, org, explicit)  # read-only
    except Refused as exc:
        out(f"REFUSED: {exc}")
        return 2

    out(f"{'APPLY' if args.apply else 'DRY-RUN'} — organisation {plan.org_id}")
    _print_plan(plan, out)
    if not args.apply:
        out("dry-run: nothing written. Re-run with --apply and CLEANUP_TEST_DATA_ENABLED=true.")
        return 0

    if delete_object is None:
        delete_object = _storage_deleter()
    result = apply_plan(session_factory, plan, delete_object=delete_object)
    out(f"applied: {result.summary()}")
    for failed in result.failed:
        out(f"  FAILED group {failed} (rolled back; re-run to retry)")
    return 1 if result.failed else 0


def _storage_deleter() -> Callable[[str], None] | None:
    try:
        from services.storage_provider.factory import get_storage_provider

        return get_storage_provider().delete_object
    except Exception:  # noqa: BLE001 — storage is best effort
        logger.warning("storage provider unavailable; receipt objects are left in place")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    sys.exit(run())
