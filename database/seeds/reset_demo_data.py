"""Reset the shared public demo organisation to its seed baseline (phase 11).

Operator tool.  Run by hand, or (phase 12, opt-in, default off) by the backend's
scheduled reset (backend/app/core/demo_reset_scheduler.py), which calls
:func:`apply` with the same safeguards.  See docs/runbooks/demo-reset.md.

  python -m database.seeds.reset_demo_data                 # dry run (default)
  DEMO_RESET_ENABLED=true \\
  python -m database.seeds.reset_demo_data --apply --org "Atlas Demo Corp"

Safeguards
----------
* Dry run is the DEFAULT: it runs in a READ ONLY transaction and prints the rows
  per table it WOULD delete / restore.  Nothing is written.
* ``--apply`` writes only when ALSO the environment variable
  ``DEMO_RESET_ENABLED=true`` is set (read from the process environment, never
  from a .env file) AND ``--org`` names the demo organisation exactly
  ("Atlas Demo Corp").  Exactly one organisation may carry that name.
* Tenant isolation is proved twice, inside the one apply transaction: (1) before
  anything is written, every foreign key in the schema that points at a row to be
  deleted is checked — any referencing row of ANOTHER organisation refuses the run;
  (2) just before COMMIT, a content hash of every other organisation's rows in every
  table is compared with the hash taken at the start — any difference rolls the whole
  reset back.  Other organisations are never touched.

What an apply does (ONE database transaction — all or nothing)
----------------------------------------------------------------
1. Deletes the demo organisation's transactional data: trips (+ segments), expense
   reports, expenses, receipts, approvals (+ steps), card transactions, calendar
   events, AI conversations (+ messages), idempotency keys and notification read
   markers.  The seed's own rows are among them; step 4 re-creates them, so every
   seeded record a visitor changed (withdrawn, decided, deleted) comes back as seeded.
2. Restores the five seeded accounts (``DEMO_GRADES`` emails) to their seed values:
   name, role, department, manager, active status, grade, and the password (re-hashed
   from SEED_DEFAULT_PASSWORD only when it no longer matches).  A missing one is
   re-created.  Then deletes every other (visitor-created) user of the demo org.
3. Restores the seeded policy rules of the stub policy to their seed definitions and
   statuses (re-creating a missing one) and deletes visitor-authored rules.  Each
   restored rule gets a ``policy_rule.demo_reset`` audit row, so rule history shows it.
4. Re-runs the idempotent seed steps (sample trip, card feed, calendar, receipts,
   reports + approvals, decided-subject repair).  The policy corpus and its RAG index
   are untouched (no reindex needed).
5. Writes one ``demo.reset`` audit row with the counts.

Audit decision: audit_log and ai_decision_logs are append-only records and are KEPT,
including rows about deleted records (their ``actor_id`` becomes NULL when the actor
was a deleted visitor account — the FK's own ON DELETE SET NULL).  The reset itself is
audited.

After the commit, receipt image objects of deleted visitor receipts are deleted from
object storage (local / MinIO / R2 via the storage service) BEST-EFFORT: a storage
failure is reported but never undoes the database reset.  The seed's own sample images
(``receipts/seed/...``) are never deleted.
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402

from backend.app.core.audit import record_audit  # noqa: E402
from backend.app.core.database import get_sessionmaker  # noqa: E402
from backend.app.core.demo import seeded_rule_ids  # noqa: E402
from backend.app.core.security import hash_password, verify_password  # noqa: E402
from config.settings import get_settings  # noqa: E402
from database.models.identity import Organization, RoleKey, User, UserStatus  # noqa: E402
from database.models.policy import Policy  # noqa: E402
from database.models.policy_rule import (  # noqa: E402
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds import seed_all  # noqa: E402
from database.seeds.policy_content import (  # noqa: E402
    ENTRY_RULES,
    POLICY_NAME,
    POLICY_VERSION,
    PROCESS_RULES,
    RULES,
)
from database.seeds.seed_demo_grades import DEMO_ORG_NAME  # noqa: E402

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from services.storage_provider.interface import StorageProvider

logger = logging.getLogger("atlas.seed.reset_demo")

ENABLE_ENV = "DEMO_RESET_ENABLED"

# Exit codes
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


# ---------------------------------------------------------------------------
# Seed baseline of the five shared accounts.  Mirrors identity_seed.seed() and
# seed_all._seed_org_chart_extras(); the grades come from seed_all.DEMO_GRADES.
# backend/tests/test_ps_demo_reset_db.py compares a reset org with a FRESH seed,
# so any drift between this table and the seed fails the build.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedAccount:
    email: str
    name: str
    role: RoleKey
    department: str
    manager_email: str | None


SEED_ACCOUNTS: tuple[SeedAccount, ...] = (
    # Managers first: the others reference them.
    SeedAccount("manager@atlas-demo.internal", "Morgan Manager", RoleKey.manager, "Finance", None),
    SeedAccount(
        "manager2@atlas-demo.internal", "Brenda Taylor", RoleKey.manager, "Operations", None
    ),
    SeedAccount(
        "employee@atlas-demo.internal",
        "Alex Employee",
        RoleKey.employee,
        "Engineering",
        "manager@atlas-demo.internal",
    ),
    SeedAccount(
        "finance@atlas-demo.internal",
        "Finley Admin",
        RoleKey.finance_admin,
        "Finance",
        "manager@atlas-demo.internal",
    ),
    SeedAccount(
        "employee2@atlas-demo.internal",
        "Carlos Rivera",
        RoleKey.employee,
        "Operations",
        "manager2@atlas-demo.internal",
    ),
)
assert {a.email for a in SEED_ACCOUNTS} == set(seed_all.DEMO_GRADES), "seed accounts drifted"
SEED_EMAILS: frozenset[str] = frozenset(a.email for a in SEED_ACCOUNTS)

# Receipt objects the seed itself uploads — never deleted from storage.
SEED_RECEIPT_KEYS: frozenset[str] = frozenset(seed_all._RECEIPT_KEYS)


# ---------------------------------------------------------------------------
# What is wiped (demo org only), children before parents.  ``{org}`` filters are
# bound parameters (:org); table / column names are fixed literals, never input.
# ---------------------------------------------------------------------------

_IN_ORG = "organization_id = :org"
_DEMO_USERS = "SELECT id FROM users WHERE organization_id = :org"

WIPE_ORDER: tuple[tuple[str, str], ...] = (
    ("notification_reads", f"user_id IN ({_DEMO_USERS})"),
    ("idempotency_keys", _IN_ORG),
    (
        "conversation_messages",
        "conversation_id IN (SELECT id FROM conversations WHERE organization_id = :org)",
    ),
    ("conversations", _IN_ORG),
    ("approval_steps", "approval_id IN (SELECT id FROM approvals WHERE organization_id = :org)"),
    ("approvals", _IN_ORG),
    ("receipts", _IN_ORG),
    ("card_transactions", _IN_ORG),
    ("calendar_events", _IN_ORG),
    ("expenses", _IN_ORG),
    ("expense_reports", _IN_ORG),
    ("trip_segments", "trip_id IN (SELECT id FROM trips WHERE organization_id = :org)"),
    ("trips", _IN_ORG),
)

# Tables without an organization_id column: (fk column, parent table) that scopes them.
_PARENT_SCOPE: dict[str, tuple[str, str]] = {
    "approval_steps": ("approval_id", "approvals"),
    "trip_segments": ("trip_id", "trips"),
    "conversation_messages": ("conversation_id", "conversations"),
    "notification_reads": ("user_id", "users"),
    "policy_rules": ("policy_id", "policies"),
    "policy_embeddings": ("policy_id", "policies"),
}


def _seed_rule_specs() -> list[tuple[dict[str, Any], PolicyRuleStatus]]:
    specs: list[tuple[dict[str, Any], PolicyRuleStatus]] = [
        (r, PolicyRuleStatus.active) for r in (*RULES, *ENTRY_RULES, *PROCESS_RULES)
    ]
    specs.append((seed_all.CAR_CAP_DRAFT_RULE, PolicyRuleStatus.draft))
    return specs


def _rule_key(rule: PolicyRule) -> tuple[str, str, str]:
    return (rule.source_clause_ref or "", rule.rule_type.value, rule.applies_to.value)


def _spec_key(spec: dict[str, Any]) -> tuple[str, str, str]:
    return (str(spec["clause_ref"]), str(spec["rule_type"]), str(spec["applies_to"]))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class ResetRefused(Exception):
    """A safeguard refused the reset (nothing was written)."""


@dataclass
class ResetReport:
    mode: str  # "dry-run" | "apply"
    deleted: dict[str, int] = field(default_factory=dict)
    restored: dict[str, int] = field(default_factory=dict)
    storage_deleted: int = 0
    storage_failed: int = 0

    def lines(self) -> list[str]:
        verb = "WOULD delete" if self.mode == "dry-run" else "deleted"
        out = [f"Demo reset ({self.mode}) — organisation {DEMO_ORG_NAME!r}"]
        for table, n in self.deleted.items():
            out.append(f"  {verb:>12} {n:6d}  {table}")
        verb = "WOULD restore" if self.mode == "dry-run" else "restored"
        for what, n in self.restored.items():
            out.append(f"  {verb:>12} {n:6d}  {what}")
        if self.mode == "apply":
            out.append(
                f"  storage objects deleted {self.storage_deleted}, "
                f"failed {self.storage_failed} (best-effort)"
            )
        else:
            out.append("  (the seed's own rows are part of the delete counts; an apply")
            out.append("   re-creates them.  Nothing was written.)")
        return out


# ---------------------------------------------------------------------------
# Org resolution + tenant-isolation proofs
# ---------------------------------------------------------------------------


def _resolve_demo_org(session: Session, *, lock: bool) -> Organization:
    stmt = sa.select(Organization).where(Organization.name == DEMO_ORG_NAME)
    if lock:
        stmt = stmt.with_for_update()
    orgs = list(session.scalars(stmt).all())
    if len(orgs) != 1:
        raise ResetRefused(
            f"expected exactly one organisation named {DEMO_ORG_NAME!r}, found {len(orgs)}"
        )
    return orgs[0]


def _tables(session: Session) -> list[str]:
    return sorted(
        session.scalars(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
        ).all()
    )


def _columns(session: Session, table: str) -> set[str]:
    return set(
        session.scalars(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).all()
    )


def _outside_org(session: Session, table: str, alias: str) -> str | None:
    """SQL predicate selecting ``alias`` rows of ``table`` that do NOT belong to :org;
    None when the table has no org scope at all (it is then hashed whole)."""
    if table == "organizations":
        return f"{alias}.id <> :org"
    if "organization_id" in _columns(session, table):
        return f"{alias}.organization_id <> :org"
    if table in _PARENT_SCOPE:
        fk, parent = _PARENT_SCOPE[table]
        return f"{alias}.{fk} NOT IN (SELECT id FROM {parent} WHERE organization_id = :org)"
    return None


# Rows an apply deletes: table -> predicate over the bare table (bound :org).  The
# seeded emails are fixed module constants (never input); visitor-authored policy
# rules are decided in Python (seed matching) and passed to _fk_proof as an id list.
_SEED_EMAIL_SQL = ", ".join(f"'{e}'" for e in sorted(SEED_EMAILS))
VISITOR_USERS = f"organization_id = :org AND lower(email) NOT IN ({_SEED_EMAIL_SQL})"
VICTIMS: dict[str, str] = {**dict(WIPE_ORDER), "users": VISITOR_USERS}


def _fk_proof(session: Session, org_id: Any, rule_victims: list[Any]) -> None:
    """Refuse when a row of ANOTHER organisation references a row the reset deletes."""
    fks = session.execute(
        sa.text(
            """
            SELECT src.relname, att.attname, tgt.relname
              FROM pg_constraint c
              JOIN pg_class src ON src.oid = c.conrelid
              JOIN pg_class tgt ON tgt.oid = c.confrelid
              JOIN pg_namespace n ON n.oid = src.relnamespace
              JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = c.conkey[1]
             WHERE c.contype = 'f' AND n.nspname = current_schema()
               AND array_length(c.conkey, 1) = 1
            """
        )
    ).all()
    victims = VICTIMS
    for src, col, tgt in fks:
        if tgt in victims:
            target_ids = f"SELECT id FROM {tgt} WHERE {victims[tgt]}"
            params: dict[str, Any] = {"org": org_id}
        elif tgt == "policy_rules" and rule_victims:
            target_ids = "SELECT id FROM policy_rules WHERE id = ANY(:rule_ids)"
            params = {"org": org_id, "rule_ids": rule_victims}
        else:
            continue
        outside = _outside_org(session, src, "s")
        if outside is None:
            outside = "TRUE"  # an unscoped table referencing a victim: any row refuses
        n = session.scalar(
            sa.text(f"SELECT count(*) FROM {src} s WHERE s.{col} IN ({target_ids}) AND {outside}"),
            params,
        )
        if n:
            raise ResetRefused(
                f"{n} row(s) of {src}.{col} outside the demo organisation reference "
                f"{tgt} rows the reset would delete — refusing (tenant isolation)"
            )


def _others_fingerprint(session: Session, org_id: Any) -> dict[str, str]:
    """md5 over every row of every table that does NOT belong to the demo org.
    Computed in the database; never printed."""
    out: dict[str, str] = {}
    for table in _tables(session):
        where = _outside_org(session, table, "t") or "TRUE"
        out[table] = str(
            session.scalar(
                sa.text(
                    f"SELECT count(*)::text || ':' || coalesce(md5(string_agg(t::text, '|' "
                    f"ORDER BY t::text)), '') FROM {table} t WHERE {where}"
                ),
                {"org": org_id},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Plan (shared by dry run and apply)
# ---------------------------------------------------------------------------


def _demo_policies(session: Session, org_id: Any) -> list[Policy]:
    return list(session.scalars(sa.select(Policy).where(Policy.organization_id == org_id)).all())


def _rule_plan(
    session: Session, org_id: Any
) -> tuple[dict[tuple[str, str, str], PolicyRule], list[PolicyRule], Policy | None]:
    """(seed key -> kept rule, rules to delete, stub policy)."""
    stub = session.scalars(
        sa.select(Policy).where(
            Policy.organization_id == org_id,
            Policy.name == POLICY_NAME,
            Policy.version == POLICY_VERSION,
        )
    ).first()
    policy_ids = [p.id for p in _demo_policies(session, org_id)]
    rules = (
        list(
            session.scalars(
                sa.select(PolicyRule)
                .where(PolicyRule.policy_id.in_(policy_ids))
                .order_by(PolicyRule.created_at, PolicyRule.id)
            ).all()
        )
        if policy_ids
        else []
    )
    seed_keys = {_spec_key(spec) for spec, _ in _seed_rule_specs()}
    # Integration review fix: the SAME provenance test as the DEMO_MODE archive
    # guard (backend/app/core/demo.py): a rule authored through the API is never
    # "the seeded one", even when it copies a seeded rule's key and the seeded
    # rule itself is gone — it is deleted and the seeded rule re-created.
    seeded = seeded_rule_ids(session, policy=stub, rules=rules) if stub is not None else set()
    kept: dict[tuple[str, str, str], PolicyRule] = {}
    for rule in rules:
        key = _rule_key(rule)
        if rule.id in seeded and key in seed_keys and key not in kept:
            kept[key] = rule  # the oldest seeded rule per seed identity is kept
    kept_ids = {r.id for r in kept.values()}
    return kept, [r for r in rules if r.id not in kept_ids], stub


def _rules_needing_restore(kept: dict[tuple[str, str, str], PolicyRule]) -> int:
    n = 0
    for spec, status in _seed_rule_specs():
        rule = kept.get(_spec_key(spec))
        if rule is None or rule.status != status or rule.definition != spec["definition"]:
            n += 1
    return n


def _accounts_needing_restore(session: Session, org_id: Any) -> int:
    pw = get_settings().SEED_DEFAULT_PASSWORD
    users = {
        u.email.lower(): u
        for u in session.scalars(
            sa.select(User).where(User.organization_id == org_id, User.email.in_(SEED_EMAILS))
        ).all()
    }
    n = 0
    for acct in SEED_ACCOUNTS:
        user = users.get(acct.email)
        manager = users.get(acct.manager_email) if acct.manager_email else None
        if (
            user is None
            or user.name != acct.name
            or user.role != acct.role
            or user.department != acct.department
            or user.status != UserStatus.active
            or user.grade != seed_all.DEMO_GRADES[acct.email]
            or user.manager_id != (manager.id if manager else None)
            or not verify_password(pw, user.password_hash)
        ):
            n += 1
    return n


def _count(session: Session, table: str, where: str, org_id: Any) -> int:
    return int(
        session.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), {"org": org_id}) or 0
    )


# ---------------------------------------------------------------------------
# Apply steps
# ---------------------------------------------------------------------------


def _restore_accounts(session: Session, org: Organization) -> list[str]:
    """Bring the five seeded accounts back to their seed values; returns restored ids."""
    pw = get_settings().SEED_DEFAULT_PASSWORD
    by_email = {
        u.email.lower(): u
        for u in session.scalars(
            sa.select(User).where(User.organization_id == org.id, User.email.in_(SEED_EMAILS))
        ).all()
    }
    restored: list[str] = []
    for acct in SEED_ACCOUNTS:
        user = by_email.get(acct.email)
        manager = by_email.get(acct.manager_email) if acct.manager_email else None
        manager_id = manager.id if manager is not None else None
        grade = seed_all.DEMO_GRADES[acct.email]
        if user is None:
            user = User(
                organization_id=org.id,
                email=acct.email,
                name=acct.name,
                role=acct.role,
                department=acct.department,
                manager_id=manager_id,
                status=UserStatus.active,
                grade=grade,
                password_hash=hash_password(pw),
            )
            session.add(user)
            session.flush()
            by_email[acct.email] = user
            restored.append(str(user.id))
            continue
        changed = False
        for attr, value in (
            ("name", acct.name),
            ("role", acct.role),
            ("department", acct.department),
            ("manager_id", manager_id),
            ("status", UserStatus.active),
            ("grade", grade),
        ):
            if getattr(user, attr) != value:
                setattr(user, attr, value)
                changed = True
        if not verify_password(pw, user.password_hash):
            user.password_hash = hash_password(pw)
            changed = True
        if changed:
            restored.append(str(user.id))
    session.flush()
    return restored


def _restore_rules(
    session: Session,
    org: Organization,
    kept: dict[tuple[str, str, str], PolicyRule],
    stub: Policy | None,
    finance_id: Any,
) -> int:
    if stub is None:
        logger.warning("Demo org has no stub policy %r — rules not restored.", POLICY_NAME)
        return 0
    restored = 0
    for spec, status in _seed_rule_specs():
        rule = kept.get(_spec_key(spec))
        definition = copy.deepcopy(spec["definition"])
        if rule is None:
            rule = PolicyRule(
                policy_id=stub.id,
                rule_type=PolicyRuleType(spec["rule_type"]),
                applies_to=PolicyAppliesTo(spec["applies_to"]),
                definition=definition,
                source_clause_ref=spec["clause_ref"],
                status=status,
                created_by=finance_id,
                updated_by=finance_id,
            )
            session.add(rule)
            session.flush()
            before: dict[str, Any] | None = None
        elif rule.status != status or rule.definition != spec["definition"]:
            before = {"status": rule.status.value, "definition": rule.definition}
            rule.status = status
            rule.definition = definition
            rule.updated_by = finance_id
            session.flush()
        else:
            continue
        restored += 1
        record_audit(
            session,
            organization_id=org.id,
            action="policy_rule.demo_reset",
            entity_type="policy_rule",
            entity_id=rule.id,
            actor_id=None,
            before_state=before,
            after_state={
                "status": status.value,
                "definition": definition,
                "source": "demo reset (database/seeds/reset_demo_data.py)",
            },
        )
    return restored


def _reseed(session: Session, org: Organization) -> None:
    """The idempotent seed steps that re-create the transactional baseline (same order
    as seed_all.seed()); flush-only — the caller commits once."""

    def _user(email: str) -> User:
        return session.scalars(
            sa.select(User).where(User.organization_id == org.id, User.email == email)
        ).one()

    employee = _user("employee@atlas-demo.internal")
    manager = _user("manager@atlas-demo.internal")
    seed_all._seed_sample_trip(session, org, employee)
    seed_all._seed_card_transactions(session, org, employee)
    seed_all._seed_calendar_events(session, org, employee)
    seed_all._seed_receipts(session, org, employee)
    manager2, employee2 = seed_all._seed_org_chart_extras(session, org)
    seed_all._seed_demo_grades(session, org)
    seed_all._seed_expense_reports_and_approvals(
        session, org, employee, manager, employee2, manager2
    )
    seed_all._reconcile_decided_subjects(session, org)
    session.flush()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def dry_run(session_factory: Callable[[], Session] | None = None) -> ResetReport:
    """Count what an apply would do, in a READ ONLY transaction (writes are impossible)."""
    factory = session_factory or get_sessionmaker()
    report = ResetReport(mode="dry-run")
    with factory() as session:
        session.execute(sa.text("SET TRANSACTION READ ONLY"))
        try:
            org = _resolve_demo_org(session, lock=False)
            kept, rule_victims, _stub = _rule_plan(session, org.id)
            _fk_proof(session, org.id, [r.id for r in rule_victims])
            for table, where in WIPE_ORDER:
                report.deleted[table] = _count(session, table, where, org.id)
            report.deleted["policy_rules (visitor-authored)"] = len(rule_victims)
            report.deleted["users (visitor-created)"] = _count(
                session, "users", VISITOR_USERS, org.id
            )
            report.restored["seeded accounts"] = _accounts_needing_restore(session, org.id)
            report.restored["seeded policy rules"] = _rules_needing_restore(kept)
        finally:
            session.rollback()
    return report


def _check_apply_allowed(org_name: str | None) -> None:
    if os.environ.get(ENABLE_ENV, "").strip().lower() != "true":
        raise ResetRefused(f"--apply needs the environment variable {ENABLE_ENV}=true")
    if org_name != DEMO_ORG_NAME:
        raise ResetRefused(f"--apply needs --org {DEMO_ORG_NAME!r} exactly")


def apply_refusal(org_name: str | None) -> str | None:
    """Why the environment / name safeguards would refuse an apply for ``org_name``
    (None = allowed).  The backend's scheduled reset uses this, so the tool and the
    scheduler share ONE check; :func:`apply` still runs it again itself."""
    try:
        _check_apply_allowed(org_name)
    except ResetRefused as exc:
        return str(exc)
    return None


def apply(
    org_name: str | None,
    *,
    session_factory: Callable[[], Session] | None = None,
    storage: StorageProvider | None = None,
) -> ResetReport:
    """Reset the demo org to the seed baseline (one transaction), then best-effort
    storage cleanup.  Raises ResetRefused (nothing written) when a safeguard fails."""
    _check_apply_allowed(org_name)
    factory = session_factory or get_sessionmaker()
    report = ResetReport(mode="apply")
    storage_keys: list[str] = []
    with factory() as session:
        try:
            org = _resolve_demo_org(session, lock=True)
            others_before = _others_fingerprint(session, org.id)
            kept, rule_victims, stub = _rule_plan(session, org.id)
            _fk_proof(session, org.id, [r.id for r in rule_victims])

            storage_keys = [
                k
                for k in session.scalars(
                    sa.text("SELECT image_url FROM receipts WHERE organization_id = :org"),
                    {"org": org.id},
                ).all()
                if k and k not in SEED_RECEIPT_KEYS
            ]

            # 1. transactional data
            for table, where in WIPE_ORDER:
                result = session.execute(
                    sa.text(f"DELETE FROM {table} WHERE {where}"), {"org": org.id}
                )
                report.deleted[table] = int(result.rowcount or 0)  # type: ignore[attr-defined]
            session.expire_all()

            # 2. seeded accounts, then visitor accounts
            restored_users = _restore_accounts(session, org)
            finance = session.scalars(
                sa.select(User).where(
                    User.organization_id == org.id,
                    User.email == "finance@atlas-demo.internal",
                )
            ).one()

            # 3. policy rules
            for rule in rule_victims:
                session.delete(rule)
            session.flush()
            report.deleted["policy_rules (visitor-authored)"] = len(rule_victims)
            report.restored["seeded policy rules"] = _restore_rules(
                session, org, kept, stub, finance.id
            )

            result = session.execute(
                sa.text(f"DELETE FROM users WHERE {VISITOR_USERS}"), {"org": org.id}
            )
            report.deleted["users (visitor-created)"] = int(result.rowcount or 0)  # type: ignore[attr-defined]
            report.restored["seeded accounts"] = len(restored_users)
            session.expire_all()

            # 4. seed baseline
            _reseed(session, org)

            # 5. audit + isolation proof, then commit
            record_audit(
                session,
                organization_id=org.id,
                action="demo.reset",
                entity_type="organization",
                entity_id=org.id,
                actor_id=None,
                before_state=None,
                after_state={
                    "deleted": dict(report.deleted),
                    "restored": dict(report.restored),
                    "restored_user_ids": restored_users,
                    "storage_objects_to_delete": len(storage_keys),
                },
            )
            if _others_fingerprint(session, org.id) != others_before:
                raise ResetRefused(
                    "another organisation's rows changed during the reset — rolled back"
                )
            session.commit()
        except BaseException:
            session.rollback()
            raise

    # 6. storage — best-effort, after the commit (the rows are already gone)
    if storage_keys:
        if storage is None:
            try:
                from services.storage_provider.factory import (  # noqa: PLC0415
                    get_storage_provider,
                )

                storage = get_storage_provider()
            except Exception:  # noqa: BLE001 — best-effort by design
                logger.warning("Storage unavailable — %d object(s) left.", len(storage_keys))
                report.storage_failed = len(storage_keys)
                return report
        for key in storage_keys:
            try:
                storage.delete_object(key)
                report.storage_deleted += 1
            except Exception as exc:  # noqa: BLE001 — best-effort by design
                report.storage_failed += 1
                logger.warning("Storage delete failed (%s).", type(exc).__name__)
    return report


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
    storage: StorageProvider | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m database.seeds.reset_demo_data",
        description="Reset the shared demo organisation to its seed baseline "
        "(dry run unless --apply).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=f"write the reset (also needs {ENABLE_ENV}=true and --org)",
    )
    parser.add_argument("--org", help=f"must be exactly {DEMO_ORG_NAME!r} with --apply")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            report = apply(args.org, session_factory=session_factory, storage=storage)
        else:
            report = dry_run(session_factory)
    except ResetRefused as exc:
        print(f"REFUSED: {exc}. Nothing was written.", file=sys.stderr)
        return EXIT_REFUSED
    except Exception:
        logger.exception("Demo reset FAILED (rolled back).")
        return EXIT_FAILED
    print("\n".join(report.lines()))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
