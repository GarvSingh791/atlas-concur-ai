"""Phase 11 — the demo reset operator tool (database/seeds/reset_demo_data.py).

Runs ONLY against its own throwaway database (``<test db>_demo_reset``), fully
migrated and seeded with the real ``seed_all``.  Storage is always a fake.

* the dry run is the default and writes nothing (whole-database hash identical);
* ``--apply`` is refused without DEMO_RESET_ENABLED=true or with a wrong / missing
  ``--org`` (nothing written);
* a TRASHED demo org (visitor users + data, seeded accounts demoted / locked /
  re-graded / re-managed / re-passworded, seeded records changed or deleted, rules
  archived / edited / activated / deleted, a visitor rule) is restored to exactly the
  FRESH-SEED baseline; another organisation is untouched (row-for-row hash);
* a re-run is idempotent; a storage failure never aborts the database phase;
* tenant isolation: a cross-tenant reference refuses the run, and any change to
  another organisation's rows during the reset rolls everything back.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

import backend.tests.factories as f
from backend.app.core.audit import record_audit
from backend.app.core.security import hash_password, verify_password
from config.settings import get_settings
from database.models.calendar_event import CalendarEvent
from database.models.card_transaction import CardTransaction
from database.models.conversation import Conversation, ConversationContextType
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.idempotency_key import IdempotencyKey
from database.models.identity import Organization, RoleKey, User, UserStatus
from database.models.notification_read import NotificationRead
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import Receipt
from database.models.trip import TripStatus
from database.seeds import reset_demo_data as rd
from database.seeds.policy_content import POLICY_NAME, POLICY_VERSION
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = "Atlas Demo Corp"
OTHER = "Other Tenant Ltd"
APPLY = ["--apply", "--org", DEMO]


# ---------------------------------------------------------------------------
# Fakes + scratch database
# ---------------------------------------------------------------------------


class FakeStorage(StorageProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        if self.fail:
            raise RuntimeError("storage down")
        self.objects.pop(key, None)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"fake://{key}", expires_in=expires_in)


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    raw_url = get_settings().DATABASE_URL
    db_name = f"{make_url(raw_url).database}_demo_reset"
    try:
        admin = create_engine(_url_with_db(raw_url, "postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping demo reset tests")

    def _drop() -> None:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))

    _drop()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    url = _url_with_db(raw_url, db_name)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    up = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert up.returncode == 0, up.stderr
    try:
        yield eng
    finally:
        eng.dispose()
        _drop()
        admin.dispose()


@pytest.fixture(scope="module")
def factory(engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(scope="module")
def baseline(engine: Engine, factory: Callable[[], Session]) -> dict[str, Any]:
    """Fresh real seed (seed_all) + another tenant; returns the fresh-seed fingerprint."""
    from database.seeds.seed_all import seed  # noqa: PLC0415

    mp = pytest.MonkeyPatch()
    mp.setenv("STORAGE_SECRET_ACCESS_KEY", "")  # the seed's MinIO upload stays off
    try:
        seed(session_factory=factory)
    finally:
        mp.undo()
    with factory() as s:
        org = s.scalars(sa.select(Organization).where(Organization.name == DEMO)).one()
        other = f.make_org(s, name=OTHER)
        twin = f.make_user(s, org=other, email="manager@atlas-demo.internal", role=RoleKey.manager)
        admin = f.make_user(s, org=other, role=RoleKey.finance_admin, manager_id=twin.id)
        trip = f.make_trip(s, traveler=twin, status=TripStatus.approved)
        f.make_trip_segment(s, trip=trip)
        report = f.make_expense_report(s, owner=admin, title="Other tenant report")
        f.make_expense(s, report=report)
        f.make_receipt(s, uploaded_by=admin, image_url=f"receipts/{other.id}/other.png")
        f.make_approval(s, submitter=admin)
        policy = f.make_policy(s, org=other, name=POLICY_NAME, version=POLICY_VERSION)
        f.make_policy_rule(
            s,
            policy=policy,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.hotel,
            source_clause_ref="HOTEL-CAP",
            status=PolicyRuleStatus.archived,
        )
        s.add(
            Conversation(
                organization_id=other.id,
                user_id=admin.id,
                context_type=ConversationContextType.general,
            )
        )
        s.add(NotificationRead(user_id=admin.id, notification_key="other-key"))
        record_audit(
            s,
            organization_id=other.id,
            action="other.event",
            entity_type="user",
            entity_id=admin.id,
            actor_id=admin.id,
        )
        s.commit()
        return {"org_id": org.id, "other_id": other.id, "fresh": _demo_fp(engine, org.id)}


@pytest.fixture(autouse=True)
def _start_from_baseline(baseline: dict[str, Any], factory: Callable[[], Session]) -> None:
    """Every test starts from the seed baseline (tests trash the org themselves)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rd.ENABLE_ENV, "true")
        rd.apply(DEMO, session_factory=factory, storage=FakeStorage())


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

# Columns that legitimately differ between two seedings (generated ids, clocks,
# random-salted hashes).  Relationships are re-expressed by natural keys below.
_VOLATILE = {"id", "created_at", "updated_at", "password_hash", "read_at", "version"}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_DEMO_TABLES: tuple[tuple[str, str], ...] = (
    *rd.WIPE_ORDER,
    ("users", "organization_id = :org"),
    ("policies", "organization_id = :org"),
    ("policy_rules", "policy_id IN (SELECT id FROM policies WHERE organization_id = :org)"),
)


def _stable_columns(conn: sa.Connection, table: str) -> list[str]:
    cols = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t ORDER BY column_name"
        ),
        {"t": table},
    ).scalars()
    return [
        c for c in cols if c not in _VOLATILE and not c.endswith("_id") and not c.endswith("_by")
    ]


def _demo_fp(engine: Engine, org_id: Any) -> dict[str, Any]:
    """Id-independent content of the demo org (what a visitor can see)."""
    out: dict[str, Any] = {}
    pw = get_settings().SEED_DEFAULT_PASSWORD
    with engine.connect() as conn:
        for table, where in _DEMO_TABLES:
            cols = _stable_columns(conn, table)
            col_sql = ", ".join(f"t.{c}::text" for c in cols)
            rows = conn.execute(
                sa.text(f"SELECT {col_sql} FROM {table} t WHERE {where}"), {"org": org_id}
            ).all()
            # Embedded references (e.g. a segment id inside a JSON note) are generated
            # ids too: normalised, like the id columns.
            out[table] = sorted(_UUID_RE.sub("<uuid>", repr(tuple(r))) for r in rows)
        out["users.rel"] = sorted(
            (email, mgr, verify_password(pw, h))
            for email, mgr, h in conn.execute(
                sa.text(
                    "SELECT u.email, m.email, u.password_hash FROM users u "
                    "LEFT JOIN users m ON m.id = u.manager_id WHERE u.organization_id = :org"
                ),
                {"org": org_id},
            ).all()
        )
        out["owners.rel"] = sorted(
            tuple(r)
            for r in conn.execute(
                sa.text(
                    "SELECT 'trip', t.origin || t.primary_destination, u.email FROM trips t "
                    "JOIN users u ON u.id = t.traveler_id WHERE t.organization_id = :org "
                    "UNION ALL SELECT 'report', r.title, u.email FROM expense_reports r "
                    "JOIN users u ON u.id = r.owner_id WHERE r.organization_id = :org "
                    "UNION ALL SELECT 'expense', e.description, r.title FROM expenses e "
                    "JOIN expense_reports r ON r.id = e.report_id WHERE e.organization_id = :org "
                    "UNION ALL SELECT 'approval', a.subject_type::text, u.email FROM approvals a "
                    "JOIN users u ON u.id = a.submitter_id WHERE a.organization_id = :org"
                ),
                {"org": org_id},
            ).all()
        )
    return out


def _org_rows_hash(engine: Engine, org_id: Any) -> dict[str, str]:
    """Row-for-row hash (ids and clocks included) of one organisation's data."""
    scoped = {
        "organizations": "id = :org",
        "trip_segments": "trip_id IN (SELECT id FROM trips WHERE organization_id = :org)",
        "approval_steps": "approval_id IN (SELECT id FROM approvals WHERE organization_id = :org)",
        "policy_rules": "policy_id IN (SELECT id FROM policies WHERE organization_id = :org)",
        "notification_reads": "user_id IN (SELECT id FROM users WHERE organization_id = :org)",
        "conversation_messages": (
            "conversation_id IN (SELECT id FROM conversations WHERE organization_id = :org)"
        ),
    }
    out: dict[str, str] = {}
    with engine.connect() as conn:
        tables = conn.execute(
            sa.text(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = 'organization_id'"
            )
        ).scalars()
        wheres = {t: "organization_id = :org" for t in tables} | scoped
        for table, where in sorted(wheres.items()):
            out[table] = str(
                conn.execute(
                    sa.text(
                        f"SELECT count(*)::text || md5(coalesce(string_agg(t::text, '|' "
                        f"ORDER BY t::text), '')) FROM {table} t WHERE {where}"
                    ),
                    {"org": org_id},
                ).scalar_one()
            )
    return out


def _db_hash(engine: Engine) -> dict[str, str]:
    """Whole-database hash (every row of every table)."""
    with engine.connect() as conn:
        tables = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
        ).scalars()
        return {
            t: str(
                conn.execute(
                    sa.text(
                        f"SELECT count(*)::text || md5(coalesce(string_agg(x::text, '|' "
                        f"ORDER BY x::text), '')) FROM {t} x"
                    )
                ).scalar_one()
            )
            for t in sorted(tables)
        }


# ---------------------------------------------------------------------------
# Trashing the demo org the way visitors could
# ---------------------------------------------------------------------------

VISITOR_KEY = "receipts/visitor/visitor-upload.png"


def _trash(factory: Callable[[], Session], storage: FakeStorage) -> None:
    with factory() as s:
        org = s.scalars(sa.select(Organization).where(Organization.name == DEMO)).one()

        def u(email: str) -> User:
            return s.scalars(
                sa.select(User).where(User.organization_id == org.id, User.email == email)
            ).one()

        employee, manager = u("employee@atlas-demo.internal"), u("manager@atlas-demo.internal")
        finance, manager2 = u("finance@atlas-demo.internal"), u("manager2@atlas-demo.internal")
        employee2 = u("employee2@atlas-demo.internal")

        # Visitor-created users and data.
        visitor = f.make_user(
            s, org=org, email="visitor.qa@example.test", role=RoleKey.finance_admin
        )
        f.make_user(s, org=org, email="lurker@example.test", manager_id=visitor.id)
        trip = f.make_trip(s, traveler=visitor)
        f.make_trip_segment(s, trip=trip)
        report = f.make_expense_report(s, owner=employee, title="Visitor junk report")
        f.make_expense(s, report=report, description="junk")
        f.make_receipt(s, uploaded_by=visitor, image_url=VISITOR_KEY)
        storage.put_object(VISITOR_KEY, b"img")
        f.make_approval(s, submitter=visitor)
        f.make_card_transaction(s, user=visitor, merchant="Visitor Mart")
        s.add(
            Conversation(
                organization_id=org.id,
                user_id=visitor.id,
                context_type=ConversationContextType.general,
            )
        )
        s.add(NotificationRead(user_id=employee.id, notification_key="approval:x"))
        s.add(
            IdempotencyKey(
                organization_id=org.id, scope="trip.create", idempotency_key="k1", entity_id=trip.id
            )
        )
        record_audit(
            s,
            organization_id=org.id,
            action="user.updated",
            entity_type="user",
            entity_id=manager.id,
            actor_id=visitor.id,
        )

        # Seeded accounts mutated.
        manager.status = UserStatus.inactive
        manager.name = "Hacked"
        employee.grade = "CSUITE"
        employee.manager_id = visitor.id
        employee.role = RoleKey.manager
        finance.password_hash = hash_password("someone-elses-password")
        manager2.department = "Gone"
        employee2.grade = None

        # Seeded records changed / deleted.
        seeded = s.scalars(
            sa.select(ExpenseReport).where(
                ExpenseReport.organization_id == org.id,
                ExpenseReport.title == "Q2 Summit Expenses",
            )
        ).one()
        seeded.status = ExpenseReportStatus.draft
        seeded_receipt = s.scalars(
            sa.select(Receipt).where(Receipt.organization_id == org.id).limit(1)
        ).one()
        s.delete(seeded_receipt)
        for tx in s.scalars(
            sa.select(CardTransaction).where(CardTransaction.organization_id == org.id)
        ):
            tx.matched = True
        s.delete(
            s.scalars(
                sa.select(CalendarEvent).where(CalendarEvent.organization_id == org.id).limit(1)
            ).one()
        )

        # Policy rules: archive, edit, activate the draft, delete one, add a visitor rule.
        stub = s.scalars(
            sa.select(Policy).where(Policy.organization_id == org.id, Policy.name == POLICY_NAME)
        ).one()
        rules = {
            r.source_clause_ref: r
            for r in s.scalars(sa.select(PolicyRule).where(PolicyRule.policy_id == stub.id))
        }
        rules["HOTEL-CAP"].status = PolicyRuleStatus.archived
        rules["MEALS-PD"].definition = {**rules["MEALS-PD"].definition, "tampered": True}
        rules["CAR-CAP-DRAFT"].status = PolicyRuleStatus.active
        s.delete(rules["AIR-06"])
        f.make_policy_rule(s, policy=stub, source_clause_ref="VISITOR-01")
        s.commit()


def _visitor_ids(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.text("SELECT id FROM users WHERE email = 'visitor.qa@example.test'")
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Safeguards
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _trash(factory, FakeStorage())
    before = _db_hash(engine)

    assert rd.main([], session_factory=factory) == rd.EXIT_OK

    assert _db_hash(engine) == before
    out = capsys.readouterr().out
    assert "dry-run" in out and "WOULD delete" in out and "Nothing was written" in out
    assert "users (visitor-created)" in out


def test_dry_run_transaction_is_read_only(
    factory: Callable[[], Session], baseline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even a write slipped into the dry-run path cannot land."""
    real = rd._fk_proof

    def _writes(session: Session, org_id: Any, rule_victims: list[Any]) -> None:
        session.execute(
            sa.text("UPDATE organizations SET name = name WHERE id = :o"), {"o": org_id}
        )
        real(session, org_id, rule_victims)

    monkeypatch.setattr(rd, "_fk_proof", _writes)
    with pytest.raises(sa.exc.DBAPIError, match="read-only"):
        rd.dry_run(factory)


def test_dry_run_counts_what_an_apply_would_do(
    factory: Callable[[], Session], baseline: dict[str, Any]
) -> None:
    _trash(factory, FakeStorage())
    report = rd.dry_run(factory)
    assert report.deleted["users (visitor-created)"] >= 2
    assert report.deleted["policy_rules (visitor-authored)"] >= 1
    assert report.restored["seeded accounts"] >= 5  # all five were mutated
    assert report.restored["seeded policy rules"] >= 4


@pytest.mark.parametrize(
    ("env", "argv"),
    [
        (None, APPLY),
        ("false", APPLY),
        ("1", APPLY),
        ("true", ["--apply"]),
        ("true", ["--apply", "--org", "atlas demo corp"]),
        ("true", ["--apply", "--org", "Atlas Demo Corp "]),
        ("true", ["--apply", "--org", OTHER]),
    ],
    ids=["no-env", "env-false", "env-1", "no-org", "org-case", "org-space", "other-org"],
)
def test_apply_refused_without_env_flag_or_exact_org(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    argv: list[str],
) -> None:
    if env is None:
        monkeypatch.delenv(rd.ENABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(rd.ENABLE_ENV, env)
    before = _db_hash(engine)
    assert rd.main(argv, session_factory=factory, storage=FakeStorage()) == rd.EXIT_REFUSED
    assert _db_hash(engine) == before


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_restores_a_trashed_demo_org_to_the_fresh_seed_baseline(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    _trash(factory, storage)
    for key in rd.SEED_RECEIPT_KEYS:
        storage.put_object(key, b"seed")
    assert _demo_fp(engine, baseline["org_id"]) != baseline["fresh"]
    other_before = _org_rows_hash(engine, baseline["other_id"])
    visitor_ids = _visitor_ids(engine)
    monkeypatch.setenv(rd.ENABLE_ENV, "true")

    assert rd.main(APPLY, session_factory=factory, storage=storage) == rd.EXIT_OK

    assert _demo_fp(engine, baseline["org_id"]) == baseline["fresh"]
    assert _org_rows_hash(engine, baseline["other_id"]) == other_before
    # Visitor storage object removed; the seed's own sample image kept.
    assert VISITOR_KEY not in storage.objects
    assert set(rd.SEED_RECEIPT_KEYS) <= set(storage.objects)
    with engine.connect() as conn:
        # Append-only audit kept (the visitor actor is now NULL), reset audited.
        kept = conn.execute(
            sa.text(
                "SELECT actor_id FROM audit_log WHERE action = 'user.updated' "
                "AND organization_id = :o"
            ),
            {"o": baseline["org_id"]},
        ).all()
        assert visitor_ids and kept and any(r[0] is None for r in kept)
        assert not any(r[0] in visitor_ids for r in kept)
        reset_row = conn.execute(
            sa.text(
                "SELECT after_state FROM audit_log WHERE action = 'demo.reset' "
                "AND organization_id = :o ORDER BY created_at DESC LIMIT 1"
            ),
            {"o": baseline["org_id"]},
        ).scalar_one()
        assert reset_row["deleted"]["users (visitor-created)"] == 2
        assert reset_row["restored"]["seeded accounts"] == 5
        history = conn.execute(
            sa.text("SELECT count(*) FROM audit_log WHERE action = 'policy_rule.demo_reset'")
        ).scalar_one()
        assert history >= 4  # archived + edited + activated + re-created rule
    # The restored seed accounts can log in with the published password again.
    with factory() as s:
        fin = s.scalars(
            sa.select(User).where(
                User.organization_id == baseline["org_id"],
                User.email == "finance@atlas-demo.internal",
            )
        ).one()
        assert verify_password(get_settings().SEED_DEFAULT_PASSWORD, fin.password_hash)


def test_apply_is_idempotent(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    first = rd.apply(DEMO, session_factory=factory, storage=FakeStorage())
    second = rd.apply(DEMO, session_factory=factory, storage=FakeStorage())
    assert _demo_fp(engine, baseline["org_id"]) == baseline["fresh"]
    assert second.restored == {"seeded policy rules": 0, "seeded accounts": 0}
    assert second.deleted["users (visitor-created)"] == 0
    assert second.deleted["policy_rules (visitor-authored)"] == 0
    assert second.deleted == first.deleted  # only the seed's own rows, re-created
    assert rd.dry_run(factory).restored == {"seeded accounts": 0, "seeded policy rules": 0}


def test_reset_removes_a_visitor_lookalike_and_restores_the_seeded_rule(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration review fix (d): the reset uses the SAME provenance test as the
    archive guard.  A visitor rule copying a seeded rule's key is deleted — even
    when the seeded rule itself is gone (it used to be adopted as "the seeded
    one") — and the seeded rule is re-created from the seed."""
    with factory() as s:
        stub = s.scalars(
            sa.select(Policy).where(
                Policy.organization_id == baseline["org_id"],
                Policy.name == POLICY_NAME,
                Policy.version == POLICY_VERSION,
            )
        ).one()
        finance = s.scalars(
            sa.select(User).where(
                User.organization_id == baseline["org_id"],
                User.email == "finance@atlas-demo.internal",
            )
        ).one()
        seeded = s.scalars(
            sa.select(PolicyRule).where(
                PolicyRule.policy_id == stub.id, PolicyRule.source_clause_ref == "HOTEL-CAP"
            )
        ).one()
        key = (seeded.rule_type, seeded.applies_to)
        s.delete(seeded)
        s.flush()
        lookalike = f.make_policy_rule(
            s,
            policy=stub,
            rule_type=key[0],
            applies_to=key[1],
            source_clause_ref="HOTEL-CAP",
            status=PolicyRuleStatus.draft,
            definition={"max_amount": 1, "currency": "USD", "per": "night"},
            created_by=finance.id,
        )
        record_audit(
            s,
            organization_id=baseline["org_id"],
            action="policy_rule.drafted",
            entity_type="policy_rule",
            entity_id=lookalike.id,
            actor_id=finance.id,
            after_state={"status": "draft", "provenance": "ai_generated"},
        )
        s.commit()
        lookalike_id = lookalike.id
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    report = rd.apply(DEMO, session_factory=factory, storage=FakeStorage())
    assert report.deleted["policy_rules (visitor-authored)"] == 1
    assert _demo_fp(engine, baseline["org_id"]) == baseline["fresh"]
    with factory() as s:
        assert s.get(PolicyRule, lookalike_id) is None
        restored = s.scalars(
            sa.select(PolicyRule).where(
                PolicyRule.policy_id == stub.id, PolicyRule.source_clause_ref == "HOTEL-CAP"
            )
        ).all()
        assert len(restored) == 1 and restored[0].status == PolicyRuleStatus.active


def test_storage_failure_never_aborts_the_database_phase(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    _trash(factory, storage)
    storage.fail = True
    monkeypatch.setenv(rd.ENABLE_ENV, "true")

    report = rd.apply(DEMO, session_factory=factory, storage=storage)

    assert report.storage_failed == 1 and report.storage_deleted == 0
    assert _demo_fp(engine, baseline["org_id"]) == baseline["fresh"]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_cross_tenant_reference_refuses_the_reset(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row of another org pointing at a demo visitor row (impossible through the API)
    would be modified by the delete's ON DELETE SET NULL — the reset refuses instead."""
    _trash(factory, FakeStorage())
    visitor = _visitor_ids(engine)[0]
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE users SET manager_id = :v WHERE organization_id = :o "
                "AND role = 'finance_admin'"
            ),
            {"v": visitor, "o": baseline["other_id"]},
        )
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    try:
        before = _db_hash(engine)
        # Refused up front by the foreign-key proof (not only by the final hash check).
        with pytest.raises(rd.ResetRefused, match="tenant isolation"):
            rd.apply(DEMO, session_factory=factory, storage=FakeStorage())
        assert rd.main(APPLY, session_factory=factory, storage=FakeStorage()) == rd.EXIT_REFUSED
        assert _db_hash(engine) == before
        with pytest.raises(rd.ResetRefused, match="tenant isolation"):
            rd.dry_run(factory)
    finally:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE users SET manager_id = (SELECT id FROM users WHERE "
                    "organization_id = :o AND role = 'manager') "
                    "WHERE organization_id = :o AND role = 'finance_admin'"
                ),
                {"o": baseline["other_id"]},
            )


def test_any_change_to_another_org_during_the_reset_rolls_everything_back(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trash(factory, FakeStorage())
    real_reseed = rd._reseed

    def _leaky_reseed(session: Session, org: Organization) -> None:
        real_reseed(session, org)
        session.execute(
            sa.text("UPDATE trips SET purpose = 'leak' WHERE organization_id = :o"),
            {"o": baseline["other_id"]},
        )

    monkeypatch.setattr(rd, "_reseed", _leaky_reseed)
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    before = _db_hash(engine)
    assert rd.main(APPLY, session_factory=factory, storage=FakeStorage()) == rd.EXIT_REFUSED
    assert _db_hash(engine) == before


def test_more_than_one_demo_named_org_refuses(
    engine: Engine,
    factory: Callable[[], Session],
    baseline: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with factory() as s:
        dup = f.make_org(s, name=DEMO)
        s.commit()
        dup_id = dup.id
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    try:
        before = _db_hash(engine)
        assert rd.main(APPLY, session_factory=factory, storage=FakeStorage()) == rd.EXIT_REFUSED
        assert _db_hash(engine) == before
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM organizations WHERE id = :i"), {"i": dup_id})
