"""C7R — POST /policy/evaluate and approval-time evaluation share ONE evidence-aware builder.

Staging re-acceptance found /policy/evaluate built every entry with
``build_eval_item(draft, segment=None)``: an attached receipt (deterministic OCR
vendor 'UNITED') was ignored, AIR-PREF printed "Supplier ''", lodging city was
always None, and RECEIPT-REQ reasons named the entry UUID.  Approval-time
evaluation used a different builder whose claim-vs-receipt vendor comparison was
an exact string match ('United Airlines' != 'UNITED').

DB-backed (shared rollback ``db_session``), real orchestrator + deterministic
policy handler, real seeded rule definitions.

Regression proof against the pre-fix code (which assertion fails, and why):
  - ``test_receipt_attached_united_is_in_policy_on_both_paths``: old endpoint ignored
    the receipt -> blocking AIR-PREF "Supplier ''"; old approval path -> same flag.
  - ``test_unverified_united_is_honest_and_blocking``: old reason "Supplier ''" and
    RECEIPT-REQ named the entry UUID.
  - ``test_lodging_city_and_nights_from_receipt``: old endpoint city None -> domestic
    180 cap on the full 600.
  - ``test_unverified_lodging_reason_is_honest``: old reason "city: None".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.context import AgentContext
from backend.app.modules.ai.runtime import get_orchestrator
from database.models.approval import ApprovalSubjectType
from database.models.expense import ExpenseCategory
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import OcrStatus
from database.seeds.policy_content import RULES

pytestmark = pytest.mark.db

API = "/api/v1"
DET = "deterministic"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture()
def real_orchestrator(app: Any, local_ai: None) -> Iterator[None]:
    app.dependency_overrides.pop(get_orchestrator, None)
    yield


@pytest.fixture()
def seeded_policy(db_session: Session, org_a: Any) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    for r in RULES:
        f.make_policy_rule(
            db_session,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )


def _report(db: Session, owner: User, total: str) -> ExpenseReport:
    report = f.make_expense_report(db, owner=owner)
    report.total_amount = Decimal(total)
    report.currency = "USD"
    db.flush()
    return report


def _entry(
    db: Session,
    report: ExpenseReport,
    *,
    category: ExpenseCategory,
    amount: str,
    vendor: str | None,
) -> Any:
    return f.make_expense(
        db,
        report=report,
        category=category,
        amount=amount,
        vendor=vendor,
        expense_date=date(2026, 6, 10),
    )


def _receipt(db: Session, owner: User, entry: Any, fields: dict, provenance: dict) -> Any:
    return f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields=fields,
        field_provenance=provenance,
    )


def _evaluate(client: Any, report: ExpenseReport) -> dict:
    r = client.post(
        f"{API}/policy/evaluate",
        json={"subject_type": "expense_report", "subject_id": str(report.id)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _approval_flags(db: Session, report: ExpenseReport, manager: User) -> tuple[list[dict], str]:
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=manager.id,
    )
    a = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db)
    return list(a.flags_snapshot), decide_route(a)


def _blocking(flags: list[dict]) -> set[tuple[str | None, str]]:
    return {
        (fl.get("source_clause_ref"), str(fl.get("reason")))
        for fl in flags
        if fl.get("severity") == "blocking"
    }


@pytest.mark.usefixtures("real_orchestrator", "seeded_policy")
class TestPolicyEvaluateEvidence:
    def test_receipt_attached_united_is_in_policy_on_both_paths(
        self,
        employee_client: Any,
        manager_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        emp = users_a["employee"]
        report = _report(db_session, emp, "320.00")
        e = _entry(
            db_session,
            report,
            category=ExpenseCategory.airfare,
            amount="320.00",
            vendor="United Airlines",
        )
        _receipt(
            db_session,
            emp,
            e,
            {"vendor": "UNITED", "amount": 320.0, "date": "2026-06-10"},
            {"vendor": DET, "amount": DET, "date": DET},
        )
        for client in (employee_client, manager_client):  # owner's evidence, any reader
            out = _evaluate(client, report)
            assert not _blocking(out["flags"]), out["flags"]
            assert out["verdict"] != "violation"
            assert not any("Supplier ''" in str(fl["reason"]) for fl in out["flags"])
        flags, route = _approval_flags(db_session, report, users_a["manager"])
        assert not _blocking(flags), flags
        assert route == "auto_approve"

    def test_unverified_united_is_honest_and_blocking(
        self,
        employee_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        """Typed vendor, no evidence: still escalates (C7-05) — honest, consistent reasons."""
        report = _report(db_session, users_a["employee"], "300.00")
        e = _entry(
            db_session,
            report,
            category=ExpenseCategory.airfare,
            amount="300.00",
            vendor="United Airlines",
        )
        out = _evaluate(employee_client, report)
        assert out["verdict"] == "violation"
        reasons = [str(fl["reason"]) for fl in out["flags"]]
        air = [fl for fl in out["flags"] if fl["source_clause_ref"] == "AIR-PREF"]
        assert air and air[0]["severity"] == "blocking"
        assert "could not be verified" in air[0]["reason"]
        assert "'United Airlines'" in air[0]["reason"]
        assert not any("Supplier ''" in x for x in reasons), reasons
        missing = [fl for fl in out["flags"] if fl["flag_type"] == "missing_receipt"]
        assert missing and "Receipt required for United Airlines" in missing[0]["reason"]
        assert not any(_UUID_RE.search(x) for x in reasons), reasons
        assert str(e.id) not in {fl["entry_ref"] for fl in out["flags"]}
        assert out["output"]["items"][0]["entry_ref"] == "United Airlines"

        flags, route = _approval_flags(db_session, report, users_a["manager"])
        assert route == "escalate"
        # One code path: the same blocking reasons on both surfaces.
        assert _blocking(flags) == _blocking(out["flags"])

    def test_lodging_city_and_nights_from_receipt(
        self,
        employee_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        emp = users_a["employee"]
        report = _report(db_session, emp, "600.00")
        e = _entry(
            db_session, report, category=ExpenseCategory.lodging, amount="600.00", vendor="Marriott"
        )
        _receipt(
            db_session,
            emp,
            e,
            {"vendor": "MARRIOTT", "amount": 600.0, "location": "NEW YORK, NY", "nights": 3},
            {"vendor": DET, "amount": DET, "location": DET},
        )
        out = _evaluate(employee_client, report)
        assert not [fl for fl in out["flags"] if fl["source_clause_ref"] == "HOTEL-CAP"], out[
            "flags"
        ]
        item = out["output"]["items"][0]
        assert item["item_type"] == "hotel" and item["in_policy"] is True
        flags, _ = _approval_flags(db_session, report, users_a["manager"])
        assert not _blocking(flags), flags

    def test_unverified_lodging_reason_is_honest(
        self,
        employee_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        report = _report(db_session, users_a["employee"], "600.00")
        _entry(
            db_session, report, category=ExpenseCategory.lodging, amount="600.00", vendor="Marriott"
        )
        out = _evaluate(employee_client, report)
        cap = [fl for fl in out["flags"] if fl["source_clause_ref"] == "HOTEL-CAP"]
        assert cap and cap[0]["severity"] == "blocking", out["flags"]
        reason = cap[0]["reason"]
        assert "None" not in reason
        assert "city could not be verified" in reason and "nights could not be verified" in reason
        flags, route = _approval_flags(db_session, report, users_a["manager"])
        assert route == "escalate"
        assert _blocking(flags) == _blocking(out["flags"])

    def test_other_users_receipt_is_not_evidence(
        self,
        manager_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        """A receipt uploaded by someone other than the report owner never verifies."""
        report = _report(db_session, users_a["employee"], "320.00")
        e = _entry(
            db_session,
            report,
            category=ExpenseCategory.airfare,
            amount="320.00",
            vendor="United Airlines",
        )
        _receipt(
            db_session,
            users_a["manager"],
            e,
            {"vendor": "UNITED", "amount": 320.0},
            {"vendor": DET, "amount": DET},
        )
        out = _evaluate(manager_client, report)
        air = [fl for fl in out["flags"] if fl["source_clause_ref"] == "AIR-PREF"]
        assert air and air[0]["severity"] == "blocking", out["flags"]
        assert out["verdict"] == "violation"

    def test_same_vendor_entries_stay_attributable(
        self,
        employee_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        """Review: two 'United Airlines' entries (one verified, one not) — each flag names
        a distinct readable entry_ref AND carries the entry UUID in entry_id."""
        emp = users_a["employee"]
        report = _report(db_session, emp, "770.00")
        ok = _entry(
            db_session,
            report,
            category=ExpenseCategory.airfare,
            amount="320.00",
            vendor="United Airlines",
        )
        _receipt(
            db_session,
            emp,
            ok,
            {"vendor": "UNITED", "amount": 320.0},
            {"vendor": DET, "amount": DET},
        )
        bad = _entry(
            db_session,
            report,
            category=ExpenseCategory.airfare,
            amount="450.00",
            vendor="United Airlines",
        )
        out = _evaluate(employee_client, report)
        items = out["output"]["items"]
        assert len({i["entry_ref"] for i in items}) == 2, items
        assert {i["entry_id"] for i in items} == {str(ok.id), str(bad.id)}
        air = [fl for fl in out["flags"] if fl["source_clause_ref"] == "AIR-PREF"]
        assert len(air) == 1 and air[0]["entry_id"] == str(bad.id), out["flags"]
        assert "450.00" in air[0]["entry_ref"]
        id_by_ref = {i["entry_ref"]: i["entry_id"] for i in items}
        assert all(fl["entry_id"] == id_by_ref[fl["entry_ref"]] for fl in out["flags"])
        blocking_ids = {fl["entry_id"] for fl in out["flags"] if fl["severity"] == "blocking"}
        assert blocking_ids == {str(bad.id)}, out["flags"]
        assert not any(_UUID_RE.search(str(fl["reason"])) for fl in out["flags"])
