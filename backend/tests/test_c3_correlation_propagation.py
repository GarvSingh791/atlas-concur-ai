"""C3 — request X-Correlation-ID propagates onto ai_decision_logs.correlation_id.

Self-skips when Postgres is unreachable (mark: db). Proves the single-point fix
in build_agent_context (defaults correlation_id to the request ContextVar) for
call sites that never pass the id explicitly:

  1. Receipt upload (POST /receipts) — real receipt pipeline with stub
     storage/OCR/LLM providers; the receipt_analysis row(s) carry the sent
     X-Correlation-ID, which is also echoed on the response header.
  2. Receipt upload WITHOUT the header — the middleware-generated id (read back
     from the response header) is what gets stored; never NULL / "".
  3. Policy evaluate (POST /policy/evaluate) — a real Orchestrator wired with
     only the policy agent (Cap 1 is deterministic, LLM-free); both the
     orchestrator row and the policy handler row carry the header.
  4. Approval trigger (POST /approvals) — capturing orchestrator stub records
     the AgentContext it receives and writes one ai_decision_logs row through
     the real write_decision_log; both carry the header.
  5. Oversized header — echoed verbatim on the response, stored truncated to
     MAX_CORRELATION_ID_LENGTH (128).
  6. Logging — the writer's "ai_decision ..." log record carries the request id
     both in its message (corr=<id>) and as record.correlation_id injected by
     backend.app.core.logging._CorrelationFilter.
  7. Every ai_decision_logs row written for this org by the module carries a
     non-empty correlation_id (no NULL leakage from any exercised path).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from ai_agents.agents import register_policy
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry
from backend.app.core.correlation import CORRELATION_ID_HEADER
from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.logging import _CorrelationFilter
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.app.modules.ai.runtime import MAX_CORRELATION_ID_LENGTH, get_orchestrator
from backend.tests.receipt_images import png_name, scenario_upload
from database.models import Base
from database.models.ai_decision_log import AiAgent, AiDecisionLog, AiRoute
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import Receipt
from database.seeds.identity_seed import seed_rbac_catalog
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.local import LocalStorageProvider

pytestmark = [pytest.mark.db]

_ORG_NAME = "C3 Correlation Test Org"
_EMPLOYEE_EMAIL = "c3-corr-employee@test.internal"
_FINANCE_EMAIL = "c3-corr-finance@test.internal"
_TEST_PW = "test-password-c3-correlation"
_WRITER_LOGGER = "ai_agents.decision_log.writer"

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, Policy.__table__),
    cast(Table, PolicyRule.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, AiDecisionLog.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ApprovalStep.__table__),
]


# ---------------------------------------------------------------------------
# OCR stub — high-confidence compliant NYC lodging (deterministic path, no LLM)
# ---------------------------------------------------------------------------


class _LodgingOcrProvider(OcrProvider):
    """Deterministic OCR: all routed keys high-confidence so no gap-fill/LLM runs."""

    def extract(self, image: bytes | str) -> OcrResult:
        return OcrResult(
            fields={
                "amount": 210.0,
                "currency": "USD",
                "date": "2026-06-10",
                "vendor": "Marriott New York",
                "location": "New York, NY",
                "expense_type": "hotel",
                "nights": 1,
                "per_night_rate": 210.0,
            },
            field_confidence={
                "amount": 0.97,
                "currency": 0.99,
                "date": 0.96,
                "vendor": 0.94,
                "location": 0.93,
                "expense_type": 0.96,
            },
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _purge_orgs(session: Any, org_ids: set[uuid.UUID] | list[uuid.UUID]) -> None:
    """Delete every row this module may have written for the given orgs (FK-safe)."""
    org_ids = list(org_ids)
    session.query(ApprovalStep).filter(
        ApprovalStep.approval_id.in_(
            session.query(Approval.id).filter(Approval.organization_id.in_(org_ids))
        )
    ).delete(synchronize_session=False)
    session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
    session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete()
    session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
    session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
    session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
    session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
    policy_ids = [
        p.id for p in session.query(Policy).filter(Policy.organization_id.in_(org_ids)).all()
    ]
    if policy_ids:
        session.query(PolicyRule).filter(PolicyRule.policy_id.in_(policy_ids)).delete()
    session.query(Policy).filter(Policy.organization_id.in_(org_ids)).delete()
    session.query(User).filter(User.organization_id.in_(org_ids)).delete()
    session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
    session.commit()


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping C3 correlation propagation tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    factory = get_sessionmaker()

    with factory() as session:
        stale = session.query(User).filter(User.email.in_([_EMPLOYEE_EMAIL, _FINANCE_EMAIL])).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            _purge_orgs(session, stale_org_ids)

    with factory() as session:
        seed_rbac_catalog(session)

        org = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org)
        session.flush()

        employee = User(
            organization_id=org.id,
            name="C3 Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hash_password(_TEST_PW),
        )
        finance = User(
            organization_id=org.id,
            name="C3 Finance",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hash_password(_TEST_PW),
        )
        session.add_all([employee, finance])
        session.flush()

        policy = Policy(
            organization_id=org.id,
            name="C3 Travel Policy",
            version=1,
            effective_from=date(2026, 1, 1),
            is_active=True,
            created_by=finance.id,
            updated_by=finance.id,
        )
        session.add(policy)
        session.flush()

        session.add(
            PolicyRule(
                policy_id=policy.id,
                rule_type=PolicyRuleType.amount_cap,
                applies_to=PolicyAppliesTo.hotel,
                source_clause_ref="HOTEL-CAP",
                status=PolicyRuleStatus.active,
                definition={
                    "currency": "USD",
                    "per": "night",
                    "tiers": [
                        {"tier": "tier1", "cities": ["New York"], "cap": 250},
                        {"tier": "domestic", "cap": 180},
                    ],
                    "exception_approver": "manager",
                },
                created_by=finance.id,
                updated_by=finance.id,
            )
        )
        session.commit()

        ids = {"org_id": org.id, "employee_id": employee.id, "finance_id": finance.id}

    yield ids

    with factory() as session:
        _purge_orgs(session, [ids["org_id"]])


@pytest.fixture(scope="module")
def client(seeded: dict[str, Any]) -> Iterator[TestClient]:
    from services.llm_provider.factory import get_llm_provider
    from services.ocr_provider.factory import get_ocr_provider
    from services.storage_provider.factory import get_storage_provider

    app.dependency_overrides[get_storage_provider] = lambda: LocalStorageProvider()
    app.dependency_overrides[get_ocr_provider] = lambda: _LodgingOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()

    yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.pop(get_storage_provider, None)
    app.dependency_overrides.pop(get_ocr_provider, None)
    app.dependency_overrides.pop(get_llm_provider, None)
    app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture
def policy_orchestrator() -> Iterator[None]:
    """Override get_orchestrator with a real Orchestrator holding only the policy agent.

    Cap 1 (policy_evaluate) is a deterministic, LLM-free handler, so the real
    orchestrator + handler write genuine ai_decision_logs rows.
    """
    registry = AgentRegistry()
    register_policy(registry, provider=LocalLlmProvider())
    orchestrator = Orchestrator(registry)
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture
def capturing_approval_orchestrator(seeded: dict[str, Any]) -> Iterator[list[AgentContext]]:
    """Override get_orchestrator with a stub that captures the AgentContext.

    The stub writes an Approval row (so the endpoint can return 201) and one
    ai_decision_logs row through the REAL write_decision_log — exactly what the
    approval agent does — without needing an LLM or a real subject.
    """
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )

    captured: list[AgentContext] = []

    def _handle_task(task: Any, *, context: AgentContext, session: Any) -> OrchestratorOutcome:
        captured.append(context)
        approval = Approval(
            organization_id=seeded["org_id"],
            subject_type=ApprovalSubjectType(task.payload["subject_type"]),
            subject_id=uuid.UUID(str(task.payload["subject_id"])),
            submitter_id=seeded["employee_id"],
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary=None,
        )
        session.add(approval)
        session.flush()
        entry = write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.approval,
                task_type="approval_decision",
                route=AiRoute.deterministic,
                input_summary={"subject_type": task.payload["subject_type"]},
                confidence=1.0,
                latency_ms=0,
            ),
        )
        decision = RoutingDecision(
            target_agent=AiAgent.approval,
            route=AiRoute.deterministic,
            task_type="approval_decision",
            confidence=1.0,
            rationale="c3 capturing stub",
        )
        result = AgentResult(
            status=AgentResultStatus.ok,
            output={"approval_id": str(approval.id), "current_status": "pending"},
            citations=(),
        )
        return OrchestratorOutcome(decision=decision, result=result, log_id=entry.id)

    stub = MagicMock()
    stub.handle_task = _handle_task
    app.dependency_overrides[get_orchestrator] = lambda: stub
    try:
        yield captured
    finally:
        app.dependency_overrides.pop(get_orchestrator, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(user_id: uuid.UUID, org_id: uuid.UUID, cid: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {create_access_token(user_id, org_id)}"}
    if cid is not None:
        headers[CORRELATION_ID_HEADER] = cid
    return headers


def _receipt_file(name: str = "c3-hotel-compliant.jpg") -> dict[str, Any]:
    return {"file": scenario_upload(name, png_name(name))}


def _rows_for_cid(org_id: uuid.UUID, cid: str) -> list[AiDecisionLog]:
    factory = get_sessionmaker()
    with factory() as session:
        return list(
            session.scalars(
                sa.select(AiDecisionLog).where(
                    AiDecisionLog.organization_id == org_id,
                    AiDecisionLog.correlation_id == cid,
                )
            ).all()
        )


def _new_cid() -> str:
    return f"c3-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# 1–2. Receipt upload path
# ---------------------------------------------------------------------------


class TestReceiptUploadCorrelation:
    def test_header_echoed_and_persisted_on_receipt_rows(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        cid = _new_cid()
        r = client.post(
            "/api/v1/receipts",
            files=_receipt_file(),
            headers=_headers(seeded["employee_id"], seeded["org_id"], cid),
        )
        assert r.status_code == 201, r.text
        assert r.headers[CORRELATION_ID_HEADER] == cid

        rows = _rows_for_cid(seeded["org_id"], cid)
        assert rows, "receipt upload wrote no ai_decision_logs row carrying the request id"
        agents = {row.agent for row in rows}
        assert AiAgent.receipt_analysis in agents
        assert all(row.correlation_id == cid for row in rows)

    def test_generated_id_persisted_when_header_absent(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/receipts",
            files=_receipt_file("c3-hotel-compliant-nohdr.jpg"),
            headers=_headers(seeded["employee_id"], seeded["org_id"]),
        )
        assert r.status_code == 201, r.text
        generated = r.headers[CORRELATION_ID_HEADER]
        assert generated
        uuid.UUID(generated)  # middleware generates a uuid4

        rows = _rows_for_cid(seeded["org_id"], generated)
        assert rows, "generated correlation id was not stored on ai_decision_logs"
        assert AiAgent.receipt_analysis in {row.agent for row in rows}

    def test_oversized_header_echoed_verbatim_but_stored_truncated(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        long_cid = "c3-long-" + "a" * 300
        r = client.post(
            "/api/v1/receipts",
            files=_receipt_file("c3-hotel-compliant-long.jpg"),
            headers=_headers(seeded["employee_id"], seeded["org_id"], long_cid),
        )
        assert r.status_code == 201, r.text
        # Middleware contract is unchanged: the header is echoed exactly.
        assert r.headers[CORRELATION_ID_HEADER] == long_cid

        stored = long_cid[:MAX_CORRELATION_ID_LENGTH]
        rows = _rows_for_cid(seeded["org_id"], stored)
        assert rows
        assert all(len(row.correlation_id or "") == MAX_CORRELATION_ID_LENGTH for row in rows)
        assert _rows_for_cid(seeded["org_id"], long_cid) == []


# ---------------------------------------------------------------------------
# 3. Policy evaluate path (real orchestrator, deterministic Cap 1)
# ---------------------------------------------------------------------------


class TestPolicyEvaluateCorrelation:
    def test_orchestrator_and_policy_rows_carry_header(
        self,
        seeded: dict[str, Any],
        client: TestClient,
        policy_orchestrator: None,
    ) -> None:
        cid = _new_cid()
        # POST /policy/evaluate validates its subject (C7-11 / C7-12): subject_type
        # must be expense_report|trip and the subject must exist in the caller's
        # read scope. Seed a draft report owned by the employee; the module
        # teardown deletes every ExpenseReport in the seeded org.
        with get_sessionmaker()() as session:
            report = ExpenseReport(
                organization_id=seeded["org_id"],
                owner_id=seeded["employee_id"],
                title="C3 evaluate subject",
                currency="USD",
                status=ExpenseReportStatus.draft,
                total_amount=0,
                created_by=seeded["employee_id"],
                updated_by=seeded["employee_id"],
            )
            session.add(report)
            session.commit()
            subject_id = report.id
        r = client.post(
            "/api/v1/policy/evaluate",
            json={
                "subject_type": "expense_report",
                "subject_id": str(subject_id),
                "context": {"item_type": "hotel", "amount_usd": 210.0, "city": "New York"},
            },
            headers=_headers(seeded["employee_id"], seeded["org_id"], cid),
        )
        assert r.status_code == 200, r.text
        assert r.headers[CORRELATION_ID_HEADER] == cid

        rows = _rows_for_cid(seeded["org_id"], cid)
        agents = {row.agent for row in rows}
        assert AiAgent.orchestrator in agents
        assert AiAgent.policy in agents
        assert all(row.user_id == seeded["employee_id"] for row in rows)


# ---------------------------------------------------------------------------
# 4. Approval path (capturing stub)
# ---------------------------------------------------------------------------


class TestApprovalCorrelation:
    def test_agent_context_and_row_carry_header(
        self,
        seeded: dict[str, Any],
        client: TestClient,
        capturing_approval_orchestrator: list[AgentContext],
    ) -> None:
        cid = _new_cid()
        # POST /approvals validates its subject before the agent runs (C7-03 /
        # C7-13): it must exist in the caller's scope, not be the caller's own,
        # and be submitted. Seed a submitted report owned by the employee.
        with get_sessionmaker()() as session:
            report = ExpenseReport(
                organization_id=seeded["org_id"],
                owner_id=seeded["employee_id"],
                title="C3 approval subject",
                currency="USD",
                status=ExpenseReportStatus.submitted,
                total_amount=0,
                created_by=seeded["employee_id"],
                updated_by=seeded["employee_id"],
            )
            session.add(report)
            session.commit()
            subject_id = report.id
        r = client.post(
            "/api/v1/approvals",
            json={"subject_type": "expense_report", "subject_id": str(subject_id)},
            headers=_headers(seeded["finance_id"], seeded["org_id"], cid),
        )
        assert r.status_code == 201, r.text
        assert r.headers[CORRELATION_ID_HEADER] == cid

        assert len(capturing_approval_orchestrator) == 1
        assert capturing_approval_orchestrator[0].correlation_id == cid

        rows = _rows_for_cid(seeded["org_id"], cid)
        assert [row.agent for row in rows] == [AiAgent.approval]


# ---------------------------------------------------------------------------
# 5. Writer log record carries the correlation id (via _CorrelationFilter)
# ---------------------------------------------------------------------------


class TestDecisionLogWriterLogging:
    def test_ai_decision_log_record_carries_request_id(
        self,
        seeded: dict[str, Any],
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cid = _new_cid()
        corr_filter = _CorrelationFilter()
        caplog.handler.addFilter(corr_filter)
        # The session conftest runs Alembic, whose env.py calls logging.config.fileConfig
        # (disable_existing_loggers=True) — that silences already-imported loggers such
        # as the writer's. Re-enable it for this test only and restore afterwards.
        writer_logger = logging.getLogger(_WRITER_LOGGER)
        was_disabled = writer_logger.disabled
        writer_logger.disabled = False
        try:
            with caplog.at_level(logging.INFO, logger=_WRITER_LOGGER):
                r = client.post(
                    "/api/v1/receipts",
                    files=_receipt_file("c3-hotel-compliant-log.jpg"),
                    headers=_headers(seeded["employee_id"], seeded["org_id"], cid),
                )
        finally:
            caplog.handler.removeFilter(corr_filter)
            writer_logger.disabled = was_disabled
        assert r.status_code == 201, r.text

        decision_records = [
            rec
            for rec in caplog.records
            if rec.name == _WRITER_LOGGER and rec.getMessage().startswith("ai_decision")
        ]
        assert decision_records, "writer emitted no ai_decision log line"
        for rec in decision_records:
            assert getattr(rec, "correlation_id", None) == cid
            assert f"corr={cid}" in rec.getMessage()


# ---------------------------------------------------------------------------
# 6. No NULL correlation_id leaked by any path exercised in this module
# ---------------------------------------------------------------------------


class TestNoNullCorrelation:
    def test_every_org_row_has_a_correlation_id(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        # Self-sufficient when run in isolation: write at least one row first.
        r = client.post(
            "/api/v1/receipts",
            files=_receipt_file("c3-hotel-compliant-nonull.jpg"),
            headers=_headers(seeded["employee_id"], seeded["org_id"]),
        )
        assert r.status_code == 201, r.text
        factory = get_sessionmaker()
        with factory() as session:
            rows = list(
                session.scalars(
                    sa.select(AiDecisionLog).where(
                        AiDecisionLog.organization_id == seeded["org_id"]
                    )
                ).all()
            )
        assert rows
        assert all(row.correlation_id for row in rows)
