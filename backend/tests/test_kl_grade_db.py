"""Known limits (AIR-04) — traveller grades end to end on Postgres.

* migration 0021 up / down on a POPULATED users table (rows kept, every grade NULL —
  nothing guessed; the CHECK refuses an off-ladder code; downgrade drops the column);
* Users admin API: grade on create / edit / list / detail, validated against the ladder,
  audited, and an admin can NEVER change their own grade (segregation of duties);
  GET /grades serves the ladder;
* AIR-04 decided from the OWNER's grade: trip approval, expense-line evaluation and the
  planner — the grade of whoever asks (an SVP approver / finance admin) never counts;
* the demo seed sets the demo grades only where none is recorded; the corpus seed
  refreshes a stale document text; grade questions retrieve the grade clauses.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents import process_checks
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import build_entry_eval_item
from ai_agents.agents.travel_agent.ranking import retrieve_and_rank
from ai_agents.agents.traveller_grade import owner_grade
from ai_agents.context import AgentContext
from ai_agents.tools.policy_eval import PolicyEvalTool
from backend.tests.test_pg2_process_rules_db import _clause, _planner_trip, _seed
from config.settings import get_settings
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.identity import Organization, RoleKey, User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType

pytestmark = pytest.mark.db

API = "/api/v1"
TODAY = date(2026, 11, 14)
STRONG = "Correct-Horse-42-battery"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id, user_id=user.id, granted_permission_keys=frozenset()
    )


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


# ---------------------------------------------------------------------------
# Migration 0021 on a populated users table (own throwaway database)
# ---------------------------------------------------------------------------


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


def _alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["alembic", *args],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[str]:
    raw_url = get_settings().DATABASE_URL
    db_name = f"{make_url(raw_url).database}_grade_mig"
    admin_url = _url_with_db(raw_url, "postgres")
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping migration 0021 test")
        return

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
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    try:
        yield url
    finally:
        _drop()
        admin.dispose()


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("users")}


def test_0021_up_and_down_on_a_populated_users_table(scratch_url: str) -> None:
    up = _alembic(["upgrade", "0020_notification_reads"], scratch_url)
    assert up.returncode == 0, up.stderr
    engine = create_engine(scratch_url)
    try:
        org = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(3)]
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO organizations (id, name, base_currency) VALUES (:i, 'O', 'USD')"
                ),
                {"i": org},
            )
            for n, uid in enumerate(ids):
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, organization_id, name, email, role, status, "
                        "password_hash) VALUES (:i, :o, :n, :e, 'employee', 'active', 'x')"
                    ),
                    {"i": uid, "o": org, "n": f"U{n}", "e": f"u{n}@example.test"},
                )

        up = _alembic(["upgrade", "0021_traveller_grades"], scratch_url)
        assert up.returncode == 0, up.stderr
        assert "grade" in _columns(engine)
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT id, grade FROM users ORDER BY name")).all()
        # Every existing user kept, and NO grade guessed for any of them.
        assert [r[0] for r in rows] == ids and all(r[1] is None for r in rows)

        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE users SET grade = 'SVP' WHERE id = :i"), {"i": ids[0]})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sa.text("UPDATE users SET grade = 'EVP' WHERE id = :i"), {"i": ids[1]})

        down = _alembic(["downgrade", "0020_notification_reads"], scratch_url)
        assert down.returncode == 0, down.stderr
        assert "grade" not in _columns(engine)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 3
            constraint = conn.execute(
                sa.text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_grade_on_ladder'")
            ).first()
        assert constraint is None

        again = _alembic(["upgrade", "head"], scratch_url)  # re-upgrade is clean
        assert again.returncode == 0, again.stderr
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Users admin API
# ---------------------------------------------------------------------------


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": "grade.hire@example.test",
        "name": "Grade Hire",
        "role": "employee",
        "password": STRONG,
    }
    body.update(overrides)
    return body


class TestAdminGrades:
    def test_create_with_a_grade_title_stores_the_code(
        self, finance_client: TestClient, db_session: Session
    ) -> None:
        resp = finance_client.post(f"{API}/users", json=_create_body(grade="Senior Vice President"))
        assert resp.status_code == 201, resp.text
        assert resp.json()["grade"] == "SVP"
        user = db_session.get(User, uuid.UUID(resp.json()["id"]))
        assert user is not None and user.grade == "SVP"
        audit = db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.entity_id == user.id, AuditLog.action == "user.created"
            )
        ).one()
        assert audit.after_state is not None and audit.after_state["grade"] == "SVP"

    def test_create_without_a_grade_is_unknown(self, finance_client: TestClient) -> None:
        resp = finance_client.post(f"{API}/users", json=_create_body())
        assert resp.status_code == 201 and resp.json()["grade"] is None

    @pytest.mark.parametrize("bad", ["EVP", "boss", "S V P"])
    def test_off_ladder_grade_is_422(self, finance_client: TestClient, bad: str) -> None:
        resp = finance_client.post(f"{API}/users", json=_create_body(grade=bad))
        assert resp.status_code == 422, resp.text
        assert "Valid grades" in resp.json()["error"]["message"]
        patch = finance_client.patch(f"{API}/users/{uuid.uuid4()}", json={"grade": bad})
        assert patch.status_code in (404, 422)

    def test_edit_another_users_grade_is_audited(
        self, finance_client: TestClient, db_session: Session, users_a: dict[str, User]
    ) -> None:
        target = users_a["manager"]
        resp = finance_client.patch(f"{API}/users/{target.id}", json={"grade": "vp"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["grade"] == "VP"
        resp = finance_client.patch(f"{API}/users/{target.id}", json={"grade": "SVP"})
        assert resp.json()["grade"] == "SVP"
        rows = db_session.scalars(
            sa.select(AuditLog)
            .where(AuditLog.entity_id == target.id, AuditLog.action == "user.updated")
            .order_by(AuditLog.created_at)
        ).all()
        assert [(r.before_state, r.after_state) for r in rows][-2:] == [
            ({"grade": None}, {"grade": "VP"}),
            ({"grade": "VP"}, {"grade": "SVP"}),
        ]
        # null clears it (unknown again), also audited.
        resp = finance_client.patch(f"{API}/users/{target.id}", json={"grade": None})
        assert resp.status_code == 200 and resp.json()["grade"] is None

    def test_admin_cannot_change_their_own_grade(
        self, finance_client: TestClient, db_session: Session, users_a: dict[str, User]
    ) -> None:
        me = users_a["finance"]
        resp = finance_client.patch(f"{API}/users/{me.id}", json={"grade": "CSUITE"})
        assert resp.status_code == 409 and _code(resp) == "SELF_GRADE_CHANGE"
        db_session.expire_all()
        fresh = db_session.get(User, me.id)
        assert fresh is not None and fresh.grade is None
        # Clearing an own grade set by someone else is a change too -> refused.
        me.grade = "DIRECTOR"
        db_session.flush()
        resp = finance_client.patch(f"{API}/users/{me.id}", json={"grade": None})
        assert resp.status_code == 409
        # Re-sending the unchanged value is a no-op, not a self-escalation.
        resp = finance_client.patch(f"{API}/users/{me.id}", json={"grade": "DIRECTOR"})
        assert resp.status_code == 200 and resp.json()["grade"] == "DIRECTOR"

    def test_list_and_detail_carry_the_grade(
        self, finance_client: TestClient, db_session: Session, users_a: dict[str, User]
    ) -> None:
        users_a["employee"].grade = "IC"
        db_session.flush()
        detail = finance_client.get(f"{API}/users/{users_a['employee'].id}")
        assert detail.json()["grade"] == "IC"
        listed = finance_client.get(f"{API}/users", params={"size": 100}).json()["items"]
        assert {u["id"]: u["grade"] for u in listed}[str(users_a["employee"].id)] == "IC"

    def test_grades_endpoint_serves_the_ladder(self, finance_client: TestClient) -> None:
        resp = finance_client.get(f"{API}/grades")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert [r["code"] for r in rows][:3] == ["CSUITE", "SVP", "VP"]
        by_code = {r["code"]: r for r in rows}
        assert by_code["SVP"]["entitlements"] == ["AIR-04", "AIR-07", "LOD-05", "NR-01"]
        assert by_code["VP"]["entitlements"] == [] and by_code["INTERN"]["entitlements"] == []

    def test_grades_endpoint_is_finance_only(self, employee_client: TestClient) -> None:
        assert employee_client.get(f"{API}/grades").status_code == 403


# ---------------------------------------------------------------------------
# owner_grade — org-scoped, fail closed
# ---------------------------------------------------------------------------


def test_owner_grade_is_org_scoped(
    db_session: Session, users_a: dict[str, User], users_b: dict[str, User]
) -> None:
    users_b["employee"].grade = "SVP"
    users_a["employee"].grade = "IC"
    db_session.flush()
    a_org = users_a["employee"].organization_id
    assert owner_grade(db_session, organization_id=a_org, user_id=users_a["employee"].id) == "IC"
    # A user of ANOTHER organization is unknown, whatever their grade.
    assert owner_grade(db_session, organization_id=a_org, user_id=users_b["employee"].id) is None
    assert owner_grade(db_session, organization_id=a_org, user_id=None) is None
    assert owner_grade(db_session, organization_id=a_org, user_id="not-a-uuid") is None


# ---------------------------------------------------------------------------
# AIR-04 at trip approval — the TRIP OWNER's grade
# ---------------------------------------------------------------------------


def _long_business_trip(db: Session, owner: User) -> Trip:
    return _planner_trip(
        db,
        owner,
        depart=TODAY + timedelta(days=40),
        cabin="business",
        hours=7.0,
        fare="1200",
        lowest="1200",
        origin="JFK",
        destination="LHR",
    )


def _assess_as(db: Session, trip: Trip, actor: User) -> tuple[list[dict], str]:
    a = _assessment(db, trip, actor)
    return list(a.flags_snapshot), decide_route(a)


def _assessment(db: Session, trip: Trip, actor: User) -> Any:
    db.expire_all()
    fresh = db.get(Trip, trip.id)
    assert fresh is not None
    return assess_risk(fresh, ApprovalSubjectType.trip, context=_ctx(actor), session=db)


class TestTripApprovalByOwnerGrade:
    def test_svp_owner_is_in_policy(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """No AIR-04 flag, no cabin violation, policy-compliant, nothing needs an approver
        (the $1,200 fare is above the $500 auto-approve ceiling, so a human still sees it
        — an amount rule, not AIR-04)."""
        owner = users_a["employee"]
        owner.grade = "SVP"
        db_session.flush()
        _seed(db_session, org_a)
        a = _assessment(db_session, _long_business_trip(db_session, owner), owner)
        flags = list(a.flags_snapshot)
        assert _clause(flags, "AIR-04") == []
        assert not [x for x in flags if x.get("rule_type") == "cabin_class"], flags
        assert a.is_policy_compliant is True and not a.requires_approver, a.policy_rationale

    @pytest.mark.parametrize("grade", ["VP", "DIRECTOR", "IC", "INTERN"])
    def test_junior_owner_escalates_with_the_cabin_exception(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User], grade: str
    ) -> None:
        owner = users_a["employee"]
        owner.grade = grade
        db_session.flush()
        _seed(db_session, org_a)
        flags, route = _assess_as(db_session, _long_business_trip(db_session, owner), owner)
        (flag,) = _clause(flags, "AIR-04")
        assert flag["requires_approver"] == "VP" and route == "escalate"

    def test_unknown_owner_grade_fails_closed(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        owner = users_a["employee"]
        _seed(db_session, org_a)
        flags, route = _assess_as(db_session, _long_business_trip(db_session, owner), owner)
        (flag,) = _clause(flags, "AIR-04")
        assert flag["requires_approver"] == "SVP" and route == "escalate"

    def test_the_evaluating_users_grade_never_counts(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """The owner is an IC; the approval runs as an SVP / C-suite finance admin."""
        owner, approver = users_a["employee"], users_a["finance"]
        owner.grade, approver.grade = "IC", "CSUITE"
        db_session.flush()
        _seed(db_session, org_a)
        flags, route = _assess_as(db_session, _long_business_trip(db_session, owner), approver)
        (flag,) = _clause(flags, "AIR-04")
        assert flag["observed"]["traveller_grade"] == "IC" and route == "escalate"
        # The AIR-CABIN segment verdict also used the owner's grade (still a violation).
        assert _clause(flags, "AIR-CABIN"), flags


# ---------------------------------------------------------------------------
# AIR-04 on an expense line — the REPORT OWNER's grade
# ---------------------------------------------------------------------------


def _owned_business_segment(db: Session, owner: User) -> Any:
    trip = f.make_trip(
        db,
        traveler=owner,
        origin="JFK",
        primary_destination="LHR",
        start_date=TODAY - timedelta(days=10),
        end_date=TODAY - timedelta(days=5),
        status=TripStatus.approved,
    )
    # Only a trip a HUMAN approved lends its itinerary to an expense line (C7-15).
    f.make_approval(
        db,
        submitter=owner,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.approved,
    )
    return f.make_trip_segment(
        db,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="United Airlines",
        amount=1200,
        currency="USD",
        details={"cabin_class": "business", "duration_hours": 8.0},
    )


def _airfare(segment_id: Any) -> DraftEntry:
    return DraftEntry(
        category="airfare",
        amount=1200.0,
        currency="USD",
        expense_date=(TODAY - timedelta(days=10)).isoformat(),
        vendor="United Airlines",
        description=None,
        status="matched",
        match_source=None,
        trip_segment_id=str(segment_id),
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


@pytest.mark.parametrize(("owner_grade_code", "in_policy"), [("SVP", True), ("IC", False)])
def test_expense_line_uses_the_owners_grade_not_the_requesters(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    owner_grade_code: str,
    in_policy: bool,
) -> None:
    owner, requester = users_a["employee"], users_a["manager"]
    owner.grade = owner_grade_code
    requester.grade = "IC" if in_policy else "CSUITE"  # the opposite answer
    db_session.flush()
    _seed(db_session, org_a)
    seg = _owned_business_segment(db_session, owner)
    item = build_entry_eval_item(
        _airfare(seg.id), idx=0, context=_ctx(requester), session=db_session, owner_id=owner.id
    )
    assert item.cabin_class == "business" and item.traveller_grade == owner_grade_code
    verdict = PolicyEvalTool().evaluate(item, context=_ctx(requester), session=db_session)
    (cabin,) = [v for v in verdict.verdicts if v.rule_type == "cabin_class"]
    assert cabin.in_policy is in_policy and "AIR-04" in cabin.reason


# ---------------------------------------------------------------------------
# AIR-04 in the planner — the requesting traveller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("grade", "in_policy"), [("SVP", True), ("VP", False), (None, False)])
def test_planner_business_option_follows_the_travellers_grade(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    grade: str | None,
    in_policy: bool,
) -> None:
    traveller = users_a["employee"]
    traveller.grade = grade
    db_session.flush()
    _seed(db_session, org_a)
    intent = SimpleNamespace(
        include_flights=True,
        include_hotels=False,
        origin="JFK",
        destination="LHR",
        departure_date=(TODAY + timedelta(days=40)).isoformat(),
        return_date=None,
    )
    ranked, _ = retrieve_and_rank(intent=intent, context=_ctx(traveller), session=db_session)
    (biz,) = [o for o in ranked if o.cabin_class == "business"]
    assert biz.duration_hours == 7.0 and biz.in_policy is in_policy
    air04 = [n for n in biz.policy_notices if n["clause_ref"] == "AIR-04"]
    if in_policy:
        assert air04 == [] and "AIR-04" in biz.policy_reason
    else:
        assert len(air04) == 1


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def test_demo_grades_only_fill_unknown_grades(db_session: Session) -> None:
    """Same mapping shape as DEMO_GRADES, on test-only emails (emails are globally unique and
    the real demo accounts may already exist in the shared test database)."""
    from config.constants.grades import grade
    from database.seeds.seed_all import DEMO_GRADES, _seed_demo_grades

    tag = uuid.uuid4().hex[:8]
    mapping = {
        f"{local.split('@')[0]}.{tag}@grades.example.test": code
        for local, code in DEMO_GRADES.items()
    }
    org = f.make_org(db_session, name="Demo Grades Org")
    users = {
        email: f.make_user(db_session, org=org, email=email, role=RoleKey.employee)
        for email in mapping
    }
    preset = next(e for e, c in mapping.items() if c == "SVP")
    users[preset].grade = "VP"  # set by an admin since
    outsider = f.make_user(db_session, org=org, email=f"real.{tag}@example.test")
    db_session.flush()

    assert _seed_demo_grades(db_session, org, mapping) == len(mapping) - 1
    assert users[preset].grade == "VP"  # never overwritten
    for email, code in mapping.items():
        if email != preset:
            assert users[email].grade == code
    assert outsider.grade is None  # only the listed accounts
    assert _seed_demo_grades(db_session, org, mapping) == 0  # idempotent
    # The real mapping: valid ladder codes, and AIR-04 demonstrable end to end.
    assert all(grade(c) is not None for c in DEMO_GRADES.values())
    assert DEMO_GRADES["manager@atlas-demo.internal"] == "SVP"


def test_corpus_seed_refreshes_a_stale_document_text(db_session: Session) -> None:
    from database.models.policy import Policy
    from database.seeds.corpus import ALL_DOCUMENTS
    from database.seeds.seed_all import _seed_corpus

    org = f.make_org(db_session, name="Corpus Refresh Org")
    admin = f.make_user(db_session, org=org, role=RoleKey.finance_admin)
    _seed_corpus(db_session, org, admin)
    policy_doc = ALL_DOCUMENTS[0]
    row = db_session.scalars(
        sa.select(Policy).where(Policy.organization_id == org.id, Policy.name == policy_doc.title)
    ).one()
    row.document_text = "stale text from before DEF-09"
    db_session.flush()
    _seed_corpus(db_session, org, admin)
    db_session.refresh(row)
    assert row.document_text == policy_doc.document_text and "DEF-09" in row.document_text
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(Policy).where(Policy.organization_id == org.id)
    )
    assert count == len(ALL_DOCUMENTS)  # refreshed in place, never duplicated


# ---------------------------------------------------------------------------
# RAG: grade questions retrieve the grade clauses (local embedder, no network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Who counts as SVP and above?", {"DEF-09", "FAQ-Q52"}),
        ("What traveler grades does the travel policy use?", {"DEF-09", "FAQ-Q51"}),
        ("I'm a Vice President, does the SVP exception apply to me?", {"FAQ-Q53", "AIR-04"}),
        ("Can I change my traveler grade myself?", {"FAQ-Q56", "DEF-10"}),
    ],
)
def test_grade_questions_retrieve_the_grade_clauses(
    db_session: Session, org_a: Organization, question: str, expected: set[str]
) -> None:
    from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
    from backend.tests.eval.test_rag_corpus_eval import _seed_corpus_for_org
    from services.embeddings_provider.factory import get_embeddings_provider

    _seed_corpus_for_org(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset())
    tool = PolicyRetrievalTool(embedder=get_embeddings_provider())
    result = tool.retrieve(
        RetrievalIntent(query=question, top_k=10), context=ctx, session=db_session
    )
    refs = {c.clause_ref for c in result.chunks if c.clause_ref}
    assert refs & expected, (question, sorted(refs))


class TestDemoGradeBootStep:
    """The policy_corpus boot path (staging) sets the demo accounts' grades."""

    def _demo_users(self, session: Session) -> tuple[Any, dict[str, Any]]:
        from database.seeds.seed_all import DEMO_GRADES

        # Earlier modules (e.g. test_c7_22) COMMIT a full seed to the shared test DB;
        # hide that demo org inside this rolled-back session so the step under test
        # resolves the org this test builds (order-independence fix, phase 11).
        session.execute(
            sa.update(Organization)
            .where(Organization.name == "Atlas Demo Corp")
            .values(name="Atlas Demo Corp (earlier module)")
        )
        org = f.make_org(session, name="Atlas Demo Corp")
        users = {
            email: f.make_user(session, org=org, email=email)
            for email in (*DEMO_GRADES, "someone.else@atlas-demo.internal")
        }
        session.flush()
        return org, users

    def test_sets_only_unset_demo_grades_and_never_overwrites(self, db_session: Session) -> None:
        from database.seeds.seed_all import DEMO_GRADES
        from database.seeds.seed_demo_grades import seed

        _, users = self._demo_users(db_session)
        users["manager@atlas-demo.internal"].grade = "DIRECTOR"  # set by an admin earlier
        db_session.flush()
        factory = lambda: _Reuse(db_session)  # noqa: E731
        assert seed(factory) == len(DEMO_GRADES) - 1
        assert users["manager@atlas-demo.internal"].grade == "DIRECTOR"
        assert users["manager2@atlas-demo.internal"].grade == "VP"
        assert users["employee@atlas-demo.internal"].grade == "IC"
        assert users["someone.else@atlas-demo.internal"].grade is None
        assert seed(factory) == 0  # idempotent

    def test_no_demo_org_is_a_no_op(self, db_session: Session) -> None:
        from database.seeds.seed_demo_grades import seed

        assert seed(lambda: _Reuse(db_session)) == 0


class _Reuse:
    """Context manager handing the test's session back (commit -> flush)."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def __enter__(self) -> Session:
        orig = self.s.commit
        self.s.commit = self.s.flush  # type: ignore[method-assign]
        self._orig = orig
        return self.s

    def __exit__(self, *a: object) -> None:
        self.s.commit = self._orig  # type: ignore[method-assign]
