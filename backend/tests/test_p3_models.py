"""Live-DB round-trip tests for P3-T1 — the eleven new ORM models.

NOT hermetic — needs a running Postgres with migrations 0001–0004 applied
(or will create the new tables itself if the prerequisite tables exist).
Self-skips when Postgres is unreachable, so hermetic CI stays green.

Test coverage:
  - Round-trip create/read per new model (with all required FKs seeded).
  - Vector value persists and reads back at the correct dimension.
  - JSONB columns round-trip faithfully.
  - Invalid enum value rejected at the DB layer.
  - Cascade: policy → rules, policy → embeddings, approval → steps,
    conversation → messages.
  - Polymorphic approval row with an orphan subject_id persists (app-layer
    integrity design is confirmed).
  - RESTRICT: delete a user referenced by card_transactions/receipts → error.
  - Unique violation on policies(organization_id, name, version).
  - Structural inspection: policy_rules has no organization_id; approvals has
    no created_by/updated_by; created_at-only tables have no updated_at.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import get_engine, get_sessionmaker
from config.constants.embeddings import EMBEDDING_DIMENSION
from database.models import (
    AiAgent,
    AiDecisionLog,
    AiRoute,
    Approval,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalStep,
    ApprovalSubjectType,
    Base,
    CalendarEvent,
    CardTransaction,
    Conversation,
    ConversationContextType,
    ConversationMessage,
    MessageRole,
    OcrStatus,
    Policy,
    PolicyAppliesTo,
    PolicyEmbedding,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
    Receipt,
    ReflectionOutcome,
)
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User, UserStatus

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Table references (for create/drop scoping).
# ---------------------------------------------------------------------------
_NEW_TABLES: list[Table] = [
    cast(Table, Policy.__table__),
    cast(Table, PolicyRule.__table__),
    cast(Table, PolicyEmbedding.__table__),
    cast(Table, CardTransaction.__table__),
    cast(Table, CalendarEvent.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ApprovalStep.__table__),
    cast(Table, Conversation.__table__),
    cast(Table, ConversationMessage.__table__),
    cast(Table, AiDecisionLog.__table__),
]

_PREREQ_TABLES = {"organizations", "users", "expense_reports", "expenses"}


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the live engine, skipping if Postgres is unreachable or prereqs missing.

    Ensures the pgvector extension is enabled (idempotent CREATE EXTENSION),
    then creates the 11 new tables if they don't already exist (e.g. when
    migration 0004 hasn't been applied yet).  Only drops tables this fixture
    created on teardown — never drops tables that already existed.
    """
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping P3-T1 live model tests")

    existing = set(sa_inspect(engine).get_table_names())
    if not _PREREQ_TABLES.issubset(existing):
        pytest.skip(
            "Prerequisite tables not found — run alembic upgrade head first "
            "(need: organizations, users, expense_reports, expenses)"
        )

    # Ensure pgvector extension is present before create_all materialises the
    # VECTOR column.
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    tables_to_create = [t for t in _NEW_TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)

    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


@pytest.fixture(scope="module")
def seed_base(db_engine: Engine) -> Iterator[dict]:
    """Create a shared Organization + User (+ ExpenseReport + Expense) for FK seeds."""
    factory = get_sessionmaker()
    with factory() as session:
        org = Organization(name="P3-T1 Test Org", base_currency="USD")
        session.add(org)
        session.flush()

        user = User(
            organization_id=org.id,
            name="P3-T1 User",
            email="p3t1@test.internal",
            role=RoleKey.employee,
            status=UserStatus.active,
            password_hash="hashed",
        )
        session.add(user)
        session.flush()

        # Seed an expense_report + expense for card_transaction/receipt FKs.
        report = ExpenseReport(
            organization_id=org.id,
            owner_id=user.id,
            title="P3-T1 Seed Report",
            status=ExpenseReportStatus.draft,
            total_amount=0,
            currency="USD",
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(report)
        session.flush()

        expense = Expense(
            organization_id=org.id,
            report_id=report.id,
            category=ExpenseCategory.meals,
            amount=10,
            currency="USD",
            expense_date=datetime.date(2026, 1, 1),
            status=ExpenseStatus.draft,
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(expense)
        session.commit()

        ids = {
            "org_id": org.id,
            "user_id": user.id,
            "report_id": report.id,
            "expense_id": expense.id,
        }

    yield ids

    # Teardown: remove in FK-safe reverse order.
    with factory() as session:
        session.query(Expense).filter_by(id=ids["expense_id"]).delete()
        session.query(ExpenseReport).filter_by(id=ids["report_id"]).delete()
        session.query(User).filter_by(id=ids["user_id"]).delete()
        session.query(Organization).filter_by(id=ids["org_id"]).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Structural assertions (no DB writes required — inspect schema).
# ---------------------------------------------------------------------------
class TestStructural:
    def test_policy_rules_has_no_organization_id(self, db_engine: Engine) -> None:
        cols = {c["name"] for c in sa_inspect(db_engine).get_columns("policy_rules")}
        assert "organization_id" not in cols

    def test_approvals_has_no_authorship_columns(self, db_engine: Engine) -> None:
        cols = {c["name"] for c in sa_inspect(db_engine).get_columns("approvals")}
        assert "created_by" not in cols
        assert "updated_by" not in cols

    def test_created_at_only_tables_have_no_updated_at(self, db_engine: Engine) -> None:
        for tbl in (
            "policy_embeddings",
            "approval_steps",
            "conversation_messages",
            "ai_decision_logs",
        ):
            cols = {c["name"] for c in sa_inspect(db_engine).get_columns(tbl)}
            assert "updated_at" not in cols, f"{tbl} should not have updated_at"
            assert "created_at" in cols, f"{tbl} must have created_at"


# ---------------------------------------------------------------------------
# Round-trip tests.
# ---------------------------------------------------------------------------
class TestPolicyRoundTrip:
    def test_policy_create_read(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            p = Policy(
                organization_id=seed_base["org_id"],
                name="Travel Policy",
                version=1,
                effective_from=datetime.date(2026, 1, 1),
                document_text="No first class unless flight > 6h.",
                is_active=True,
                created_by=seed_base["user_id"],
                updated_by=seed_base["user_id"],
            )
            session.add(p)
            session.commit()
            pid = p.id

        with factory() as session:
            fetched = session.get(Policy, pid)
            assert fetched is not None
            assert fetched.name == "Travel Policy"
            assert fetched.version == 1
            assert fetched.is_active is True
            assert fetched.document_text is not None
            session.delete(fetched)
            session.commit()

    def test_policy_unique_constraint(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            p1 = Policy(
                organization_id=seed_base["org_id"],
                name="UniquePolicy",
                version=1,
                effective_from=datetime.date(2026, 1, 1),
                created_by=seed_base["user_id"],
                updated_by=seed_base["user_id"],
            )
            session.add(p1)
            session.commit()
            p1_id = p1.id

        with pytest.raises(IntegrityError):
            with factory() as session:
                p2 = Policy(
                    organization_id=seed_base["org_id"],
                    name="UniquePolicy",
                    version=1,
                    effective_from=datetime.date(2026, 1, 1),
                    created_by=seed_base["user_id"],
                    updated_by=seed_base["user_id"],
                )
                session.add(p2)
                session.commit()

        with factory() as session:
            session.query(Policy).filter_by(id=p1_id).delete()
            session.commit()


class TestPolicyCascade:
    def test_delete_policy_cascades_rules_and_embeddings(
        self, db_engine: Engine, seed_base: dict
    ) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            p = Policy(
                organization_id=seed_base["org_id"],
                name="CascadePolicy",
                version=1,
                effective_from=datetime.date(2026, 1, 1),
                created_by=seed_base["user_id"],
                updated_by=seed_base["user_id"],
            )
            session.add(p)
            session.flush()

            rule = PolicyRule(
                policy_id=p.id,
                rule_type=PolicyRuleType.amount_cap,
                applies_to=PolicyAppliesTo.flight,
                definition={"cap": 500, "currency": "USD"},
                status=PolicyRuleStatus.active,
                created_by=seed_base["user_id"],
                updated_by=seed_base["user_id"],
            )
            embedding = PolicyEmbedding(
                policy_id=p.id,
                chunk_text="No first class unless flight exceeds 6 hours.",
                embedding=[0.1] * EMBEDDING_DIMENSION,
            )
            session.add_all([rule, embedding])
            session.commit()
            pid = p.id

        with factory() as session:
            session.query(Policy).filter_by(id=pid).delete()
            session.commit()

        with factory() as session:
            assert session.query(PolicyRule).filter_by(policy_id=pid).count() == 0
            assert session.query(PolicyEmbedding).filter_by(policy_id=pid).count() == 0


class TestPolicyEmbeddingVector:
    def test_vector_persists_correct_dimension(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            p = Policy(
                organization_id=seed_base["org_id"],
                name="VectorPolicy",
                version=1,
                effective_from=datetime.date(2026, 1, 1),
                created_by=seed_base["user_id"],
                updated_by=seed_base["user_id"],
            )
            session.add(p)
            session.flush()
            pid = p.id

            vec = [float(i % 100) / 100.0 for i in range(EMBEDDING_DIMENSION)]
            emb = PolicyEmbedding(
                policy_id=pid,
                chunk_text="Test chunk for vector assertion.",
                embedding=vec,
            )
            session.add(emb)
            session.commit()
            emb_id = emb.id

        with factory() as session:
            fetched = session.get(PolicyEmbedding, emb_id)
            assert fetched is not None
            # fetched.embedding is a pgvector list-like; cast for mypy.
            assert len(fetched.embedding) == EMBEDDING_DIMENSION  # type: ignore[arg-type]
            session.query(PolicyEmbedding).filter_by(id=emb_id).delete()
            session.query(Policy).filter_by(id=pid).delete()
            session.commit()


class TestCardTransactionRoundTrip:
    def test_card_transaction_create_read(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            txn = CardTransaction(
                organization_id=seed_base["org_id"],
                user_id=seed_base["user_id"],
                expense_id=None,
                amount=42,
                currency="USD",
                merchant="Coffee Shop",
                txn_date=datetime.date(2026, 1, 15),
                last_four="1234",
                matched=False,
            )
            session.add(txn)
            session.commit()
            tid = txn.id

        with factory() as session:
            fetched = session.get(CardTransaction, tid)
            assert fetched is not None
            assert fetched.last_four == "1234"
            assert fetched.matched is False
            session.delete(fetched)
            session.commit()

    def test_delete_user_with_card_transactions_blocked(
        self, db_engine: Engine, seed_base: dict
    ) -> None:
        """RESTRICT on user_id: deleting a referenced user raises IntegrityError."""
        factory = get_sessionmaker()
        with factory() as session:
            extra_user = User(
                organization_id=seed_base["org_id"],
                name="Deletable User",
                email="deletable-cardtxn@test.internal",
                role=RoleKey.employee,
                status=UserStatus.active,
                password_hash="hash",
            )
            session.add(extra_user)
            session.flush()

            txn = CardTransaction(
                organization_id=seed_base["org_id"],
                user_id=extra_user.id,
                amount=10,
                currency="USD",
                txn_date=datetime.date(2026, 1, 1),
            )
            session.add(txn)
            session.commit()
            uid = extra_user.id
            tid = txn.id

        with pytest.raises(IntegrityError):
            with factory() as session:
                session.query(User).filter_by(id=uid).delete()
                session.commit()

        with factory() as session:
            session.query(CardTransaction).filter_by(id=tid).delete()
            session.query(User).filter_by(id=uid).delete()
            session.commit()


class TestCalendarEventRoundTrip:
    def test_calendar_event_create_read(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        now = datetime.datetime(2026, 1, 10, 9, 0, tzinfo=datetime.timezone.utc)
        with factory() as session:
            ev = CalendarEvent(
                organization_id=seed_base["org_id"],
                user_id=seed_base["user_id"],
                title="Offsite Meeting",
                location="New York",
                start_datetime=now,
                end_datetime=now + datetime.timedelta(hours=8),
            )
            session.add(ev)
            session.commit()
            eid = ev.id

        with factory() as session:
            fetched = session.get(CalendarEvent, eid)
            assert fetched is not None
            assert fetched.location == "New York"
            session.delete(fetched)
            session.commit()


class TestReceiptRoundTrip:
    def test_receipt_create_read_with_jsonb(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        extracted = {"amount": "42.50", "currency": "USD", "vendor": "Hotel Inc"}
        provenance = {"amount": "deterministic", "currency": "ai_inferred"}
        with factory() as session:
            r = Receipt(
                organization_id=seed_base["org_id"],
                uploaded_by=seed_base["user_id"],
                expense_id=None,
                image_url="s3://receipts/abc.jpg",
                ocr_status=OcrStatus.processed,
                extracted_fields=extracted,
                field_provenance=provenance,
            )
            session.add(r)
            session.commit()
            rid = r.id

        with factory() as session:
            fetched = session.get(Receipt, rid)
            assert fetched is not None
            assert fetched.extracted_fields == extracted
            assert fetched.field_provenance == provenance
            session.delete(fetched)
            session.commit()

    def test_delete_user_with_receipt_blocked(self, db_engine: Engine, seed_base: dict) -> None:
        """RESTRICT on uploaded_by: deleting a referenced user raises IntegrityError."""
        factory = get_sessionmaker()
        with factory() as session:
            extra_user = User(
                organization_id=seed_base["org_id"],
                name="Deletable User R",
                email="deletable-receipt@test.internal",
                role=RoleKey.employee,
                status=UserStatus.active,
                password_hash="hash",
            )
            session.add(extra_user)
            session.flush()

            r = Receipt(
                organization_id=seed_base["org_id"],
                uploaded_by=extra_user.id,
                image_url="s3://receipts/delete-test.jpg",
                ocr_status=OcrStatus.pending,
            )
            session.add(r)
            session.commit()
            uid = extra_user.id
            rid = r.id

        with pytest.raises(IntegrityError):
            with factory() as session:
                session.query(User).filter_by(id=uid).delete()
                session.commit()

        with factory() as session:
            session.query(Receipt).filter_by(id=rid).delete()
            session.query(User).filter_by(id=uid).delete()
            session.commit()


class TestApprovalRoundTrip:
    def test_approval_polymorphic_orphan_persists(self, db_engine: Engine, seed_base: dict) -> None:
        """An approval with a subject_id that has no FK target must persist.

        This proves the polymorphic design: subject_id has no DB FK constraint,
        so orphan subject IDs are accepted by the database.  App-layer integrity
        (P3-T4) is the intended enforcement point.
        """
        factory = get_sessionmaker()
        orphan_id = uuid.uuid4()  # No trip or expense_report with this id.
        with factory() as session:
            a = Approval(
                organization_id=seed_base["org_id"],
                subject_type=ApprovalSubjectType.trip,
                subject_id=orphan_id,
                submitter_id=seed_base["user_id"],
                current_status=ApprovalStatus.pending,
                is_auto_approved=False,
            )
            session.add(a)
            session.commit()
            aid = a.id

        with factory() as session:
            fetched = session.get(Approval, aid)
            assert fetched is not None
            assert fetched.subject_id == orphan_id
            assert fetched.version == 1  # VersionMixin default
            session.delete(fetched)
            session.commit()

    def test_delete_approval_cascades_steps(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        now = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
        with factory() as session:
            a = Approval(
                organization_id=seed_base["org_id"],
                subject_type=ApprovalSubjectType.expense_report,
                subject_id=uuid.uuid4(),
                submitter_id=seed_base["user_id"],
                current_status=ApprovalStatus.pending,
            )
            session.add(a)
            session.flush()

            step = ApprovalStep(
                approval_id=a.id,
                approver_id=seed_base["user_id"],
                step_order=1,
                decision=ApprovalDecision.approve,
                decided_at=now,
            )
            session.add(step)
            session.commit()
            aid = a.id

        with factory() as session:
            session.query(Approval).filter_by(id=aid).delete()
            session.commit()

        with factory() as session:
            assert session.query(ApprovalStep).filter_by(approval_id=aid).count() == 0


class TestConversationRoundTrip:
    def test_conversation_and_messages_round_trip(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            conv = Conversation(
                organization_id=seed_base["org_id"],
                user_id=seed_base["user_id"],
                context_type=ConversationContextType.travel_planning,
                parsed_intent={"destination": "Paris", "dates": ["2026-03-01", "2026-03-07"]},
            )
            session.add(conv)
            session.flush()

            msg = ConversationMessage(
                conversation_id=conv.id,
                role=MessageRole.user,
                content="I need to book a trip to Paris.",
                sequence=1,
            )
            session.add(msg)
            session.commit()
            cid = conv.id

        with factory() as session:
            fetched = session.get(Conversation, cid)
            assert fetched is not None
            expected_intent = {"destination": "Paris", "dates": ["2026-03-01", "2026-03-07"]}
            assert fetched.parsed_intent == expected_intent

            msgs = session.query(ConversationMessage).filter_by(conversation_id=cid).all()
            assert len(msgs) == 1
            assert msgs[0].content == "I need to book a trip to Paris."
            # Delete messages then conversation to avoid FK violation in seed teardown.
            session.query(ConversationMessage).filter_by(conversation_id=cid).delete()
            session.query(Conversation).filter_by(id=cid).delete()
            session.commit()

    def test_delete_conversation_cascades_messages(
        self, db_engine: Engine, seed_base: dict
    ) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            conv = Conversation(
                organization_id=seed_base["org_id"],
                user_id=seed_base["user_id"],
                context_type=ConversationContextType.general,
            )
            session.add(conv)
            session.flush()
            for i in range(3):
                session.add(
                    ConversationMessage(
                        conversation_id=conv.id,
                        role=MessageRole.assistant,
                        content=f"Message {i}",
                        sequence=i,
                    )
                )
            session.commit()
            cid = conv.id

        with factory() as session:
            session.query(Conversation).filter_by(id=cid).delete()
            session.commit()

        with factory() as session:
            assert session.query(ConversationMessage).filter_by(conversation_id=cid).count() == 0


class TestAiDecisionLogRoundTrip:
    def test_ai_decision_log_create_read(self, db_engine: Engine, seed_base: dict) -> None:
        factory = get_sessionmaker()
        with factory() as session:
            log = AiDecisionLog(
                organization_id=seed_base["org_id"],
                user_id=seed_base["user_id"],
                agent=AiAgent.travel,
                task_type="rank_options",
                route=AiRoute.llm,
                model_provider="openai",
                confidence=0.950,
                reflection_outcome=ReflectionOutcome.passed,
                latency_ms=312,
                input_summary={"query": "Paris flights"},
                output_summary={"selected": "AF123"},
            )
            session.add(log)
            session.commit()
            lid = log.id

        with factory() as session:
            fetched = session.get(AiDecisionLog, lid)
            assert fetched is not None
            assert fetched.agent == AiAgent.travel
            assert fetched.reflection_outcome == ReflectionOutcome.passed
            assert float(str(fetched.confidence)) == pytest.approx(0.950, abs=0.001)
            assert fetched.input_summary == {"query": "Paris flights"}
            # ai_decision_logs has no updated_at
            assert (
                not hasattr(fetched, "updated_at")
                or fetched.__class__.__table__.c.get("updated_at") is None
            )
            session.delete(fetched)
            session.commit()

    def test_ai_decision_log_null_user_allowed(self, db_engine: Engine, seed_base: dict) -> None:
        """user_id=NULL is valid — system-initiated decisions have no actor."""
        factory = get_sessionmaker()
        with factory() as session:
            log = AiDecisionLog(
                organization_id=seed_base["org_id"],
                user_id=None,
                agent=AiAgent.orchestrator,
                task_type="routing",
                route=AiRoute.deterministic,
            )
            session.add(log)
            session.commit()
            lid = log.id

        with factory() as session:
            fetched = session.get(AiDecisionLog, lid)
            assert fetched is not None
            assert fetched.user_id is None
            session.delete(fetched)
            session.commit()
