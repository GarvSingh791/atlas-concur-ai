"""Issue 5 — AI policy-rule authoring end to end (real router + orchestrator + PolicyAgent,
scripted LLM, rollback DB).

Pinned:
  * the retest sentence drafts a faithful ground-transport cap (201) with a title, an
    engine-derived summary and a ``policy_rule.drafted`` audit row carrying the intent;
  * a refused draft returns a STRUCTURED error (RULE_NOT_EXPRESSIBLE / RULE_CRITIC_REJECTED
    / RULE_INVALID 422, PROVIDER_UNAVAILABLE 503) whose message carries the reason, writes
    NO rule row and commits a ``policy_rule.draft_failed`` audit row (intent <= 500 chars);
  * GET /policy-rules carries title + summary and survives malformed legacy JSON;
  * activation refuses a definition the engine would mis-enforce (422 RULE_INVALID, status
    unchanged, no audit); activate / archive audits carry the rule identity;
  * the active category-scoped cap is enforced at pre-submit AND approval time: only
    ground-transport lines are flagged (no noise on other lines), a justification makes
    the flag advisory, and the report is still escalated (never auto-approved).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.presubmit import draft_entries_for_expenses, run_presubmit
from ai_agents.context import AgentContext
from backend.app.modules.ai import runtime
from backend.app.modules.ai.runtime import get_orchestrator
from database.models.approval import ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory
from database.models.identity import User
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.seed_all import CAR_CAP_DRAFT_RULE
from services.exceptions import ProviderError
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

pytestmark = pytest.mark.db

API = "/api/v1"
RETEST = "Rideshare trips over $75 need a written business justification"
GROUND_DEFINITION: dict[str, Any] = {
    "currency": "USD",
    "per": "item",
    "expense_categories": ["ground_transport"],
    "tiers": [{"tier": "standard", "cap": 75}],
    "exception_approver": "manager",
}
GROUND_CAP = {
    "rule_type": "amount_cap",
    "applies_to": "all",
    "definition": GROUND_DEFINITION,
    "source_clause_ref": None,
}


class _Scripted(LlmProvider):
    def __init__(self, *responses: str | Exception) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.append(messages[-1].content)
        reply = self._responses[(len(self.prompts) - 1) % len(self._responses)]
        if isinstance(reply, Exception):
            raise reply
        return LlmResponse(
            text=reply, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1)
        )

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


@pytest.fixture()
def author(app: Any, local_ai: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _install(*responses: str | Exception) -> _Scripted:
        llm = _Scripted(*responses)
        monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
        runtime.build_orchestrator.cache_clear()
        app.dependency_overrides.pop(get_orchestrator, None)
        return llm

    yield _install
    runtime.build_orchestrator.cache_clear()


def _audits(db: Session, org_id: uuid.UUID, action: str) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog)
            .where(AuditLog.organization_id == org_id, AuditLog.action == action)
            .order_by(AuditLog.created_at)
        ).all()
    )


def _rule_count(db: Session, policy_id: uuid.UUID) -> int:
    return int(db.scalar(sa.select(sa.func.count()).where(PolicyRule.policy_id == policy_id)) or 0)


# ---------------------------------------------------------------------------
# Authoring: success + structured failures
# ---------------------------------------------------------------------------


def test_retest_sentence_drafts_faithful_ground_transport_cap(
    author: Any, finance_client: Any, db_session: Session, org_a: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    author(json.dumps(GROUND_CAP), "FAITHFUL\nCovers rideshare at $75 with a justification.")

    r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": RETEST})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["rule_type"] == "amount_cap" and body["applies_to"] == "all"
    assert body["definition"]["expense_categories"] == ["ground_transport"]
    assert body["definition"]["tiers"][0]["cap"] == 75
    assert body["definition"]["exception_approver"] == "manager"
    # Product gap 8 (intended change): the rideshare intent is narrowed to the
    # rideshare/taxi sub-type — trains, rentals, transit and parking are not capped.
    assert body["definition"]["ground_subtypes"] == ["rideshare_taxi"]
    assert body["title"] == "Rideshare/taxi cap — Standard $75/item"
    summary = " ".join(body["summary"])
    assert "Applies only to rideshare/taxi ground-transport expense lines" in summary
    # Product gap 8 review round 3: only lines whose kind the evidence confirms.
    assert "car rental, rail, public transit, parking lines confirmed by that evidence" in summary
    assert "until the employee adds a written justification" in summary
    (audit,) = _audits(db_session, org_a.id, "policy_rule.drafted")
    assert str(audit.entity_id) == body["id"]
    after = audit.after_state or {}
    assert after["intent"] == RETEST and after["intent_len"] == len(RETEST)
    assert after["title"] == body["title"]
    # UAT2-G: the response echoes the intent that produced THIS draft (the same text the
    # drafted audit stores), so the UI never labels a draft with a later textarea value.
    assert body["intent"] == RETEST == after["intent"]
    assert _audits(db_session, org_a.id, "policy_rule.draft_failed") == []


@pytest.mark.parametrize(
    ("responses", "status", "code", "needle"),
    [
        (
            (json.dumps({"unsupported": True, "reason": "Atlas has no rail target."}),),
            422,
            "RULE_NOT_EXPRESSIBLE",
            "Atlas can't express this rule yet: Atlas has no rail target.",
        ),
        (
            (json.dumps(GROUND_CAP), "UNFAITHFUL. The intent caps rideshare at $50, not $75."),
            422,
            "RULE_CRITIC_REJECTED",
            "The intent caps rideshare at $50, not $75.",
        ),
        (
            (json.dumps({**GROUND_CAP, "definition": {"tiers": [], "bogus": 1}}),),
            422,
            "RULE_INVALID",
            "'bogus' is not used by the rule engine",
        ),
        (
            (ProviderError("upstream down", code="LLM_TIMEOUT"),),
            503,
            "PROVIDER_UNAVAILABLE",
            "unavailable right now",
        ),
    ],
)
def test_refused_draft_returns_structured_reason_and_failed_audit(
    author: Any,
    finance_client: Any,
    db_session: Session,
    org_a: Any,
    users_a: dict[str, User],
    responses: tuple[Any, ...],
    status: int,
    code: str,
    needle: str,
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    before = _rule_count(db_session, policy.id)
    author(*responses)
    description = "Applies company-wide. " + "x" * 700

    r = finance_client.post(
        f"{API}/policies/{policy.id}/rules", json={"title": RETEST, "description": description}
    )

    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code
    assert needle in err["message"]
    assert err["correlation_id"]
    assert _rule_count(db_session, policy.id) == before
    assert _audits(db_session, org_a.id, "policy_rule.drafted") == []
    (failed,) = _audits(db_session, org_a.id, "policy_rule.draft_failed")
    assert failed.entity_type == "policy" and failed.entity_id == policy.id
    assert failed.actor_id == users_a["finance"].id
    after = failed.after_state or {}
    assert after["error_code"] == code
    assert after["provenance"] == "ai_generated"
    assert after["intent"].startswith(RETEST) and len(after["intent"]) == 500
    assert after["intent_len"] > 500
    assert after["ai_decision_log_id"]


def test_draft_failed_audit_is_org_scoped(
    author: Any, finance_client: Any, db_session: Session, org_a: Any, org_b: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    author(json.dumps({"unsupported": True, "reason": "nope"}))
    r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": RETEST})
    assert r.status_code == 422
    assert len(_audits(db_session, org_a.id, "policy_rule.draft_failed")) == 1
    assert _audits(db_session, org_b.id, "policy_rule.draft_failed") == []


def test_other_orgs_policy_is_404_and_never_audited(
    author: Any, finance_client: Any, db_session: Session, org_a: Any, org_b: Any
) -> None:
    foreign = f.make_policy(db_session, org=org_b, is_active=True)
    llm = author(json.dumps(GROUND_CAP), "FAITHFUL\nok")
    r = finance_client.post(f"{API}/policies/{foreign.id}/rules", json={"title": RETEST})
    assert r.status_code == 404
    assert llm.prompts == []
    assert _audits(db_session, org_a.id, "policy_rule.draft_failed") == []


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


def test_rule_list_carries_title_and_summary_and_survives_malformed_json(
    finance_client: Any, db_session: Session, org_a: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.meals,
        definition={"cap": 50},
    )
    f.make_policy_rule(db_session, policy=policy, definition={"tiers": [None, "x"]})
    f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.all,
        definition=GROUND_DEFINITION,
        status=PolicyRuleStatus.draft,
    )

    r = finance_client.get(f"{API}/policy-rules")

    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 3
    for row in rows:
        assert isinstance(row["title"], str) and row["title"]
        assert row["summary"] and all(isinstance(s, str) for s in row["summary"])
    ground = next(row for row in rows if row["status"] == "draft")
    assert ground["title"] == "Ground transport cap — Standard $75/item"
    legacy = [row for row in rows if row["status"] == "active"]
    assert all(row["summary"][0].startswith("Warning:") for row in legacy)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_activation_refuses_invalid_definition(
    finance_client: Any, db_session: Session, org_a: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    rule = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.all,
        definition={"max_amount": 500},
        status=PolicyRuleStatus.draft,
    )

    r = finance_client.patch(f"{API}/policy-rules/{rule.id}", json={"status": "active"})

    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "RULE_INVALID"
    assert err["message"].startswith("This rule can't be activated:")
    db_session.refresh(rule)
    assert rule.status is PolicyRuleStatus.draft
    assert _audits(db_session, org_a.id, "policy_rule.activated") == []
    # An invalid draft can still be archived (discarded).
    r = finance_client.patch(f"{API}/policy-rules/{rule.id}", json={"status": "archived"})
    assert r.status_code == 200, r.text


def test_seeded_car_cap_draft_activates_and_audits_rule_identity(
    finance_client: Any, db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    draft = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType(CAR_CAP_DRAFT_RULE["rule_type"]),
        applies_to=PolicyAppliesTo(CAR_CAP_DRAFT_RULE["applies_to"]),
        definition=CAR_CAP_DRAFT_RULE["definition"],
        status=PolicyRuleStatus.draft,
    )

    r = finance_client.patch(f"{API}/policy-rules/{draft.id}", json={"status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert r.json()["title"] == "Car rental cap — Standard $75/day"
    (activated,) = _audits(db_session, org_a.id, "policy_rule.activated")
    after = activated.after_state or {}
    assert after["rule_type"] == "amount_cap" and after["applies_to"] == "car"
    assert after["title"] == "Car rental cap — Standard $75/day"
    assert any("advisory only" in s for s in after["summary"])

    r = finance_client.patch(f"{API}/policy-rules/{draft.id}", json={"status": "archived"})
    assert r.status_code == 200, r.text
    (archived,) = _audits(db_session, org_a.id, "policy_rule.archived")
    assert (archived.before_state or {})["status"] == "active"
    assert (archived.after_state or {})["title"] == "Car rental cap — Standard $75/day"
    # Archived stays terminal.
    r = finance_client.patch(f"{API}/policy-rules/{draft.id}", json={"status": "active"})
    assert r.status_code == 422


def test_manager_and_employee_cannot_patch_rule_status(
    employee_client: Any, manager_client: Any, db_session: Session, org_a: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    draft = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.all,
        definition=GROUND_DEFINITION,
        status=PolicyRuleStatus.draft,
    )
    for client in (employee_client, manager_client):
        r = client.patch(f"{API}/policy-rules/{draft.id}", json={"status": "active"})
        assert r.status_code == 403, r.text
    db_session.refresh(draft)
    assert draft.status is PolicyRuleStatus.draft


# ---------------------------------------------------------------------------
# Enforcement of the category-scoped cap (Option B)
# ---------------------------------------------------------------------------


def _ground_report(db: Session, org: Any, owner: User) -> tuple[Any, Expense]:
    policy = f.make_policy(db, org=org, is_active=True)
    f.make_policy_rule(
        db,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.all,
        definition=GROUND_DEFINITION,
        status=PolicyRuleStatus.active,
    )
    report = f.make_expense_report(db, owner=owner, title="Client visit")
    ride = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.ground_transport,
        amount="92.00",
        vendor="Uber",
    )
    f.make_expense(db, report=report, category=ExpenseCategory.airfare, amount="900.00")
    f.make_expense(db, report=report, category=ExpenseCategory.meals, amount="140.00")
    f.make_expense(db, report=report, category=ExpenseCategory.other, amount="300.00")
    f.make_expense(
        db, report=report, category=ExpenseCategory.ground_transport, amount="40.00", vendor="Lyft"
    )
    return report, ride


def _presubmit(db: Session, report: Any, owner: User) -> list[Any]:
    expenses = list(db.scalars(sa.select(Expense).where(Expense.report_id == report.id)).all())
    entries = draft_entries_for_expenses(
        expenses, session=db, organization_id=report.organization_id
    )
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=owner.id,
    )
    flags, _ = run_presubmit(
        DraftReport(
            entries=entries, total_amount=0.0, currency="USD", trip_id=None, title=report.title
        ),
        context=ctx,
        session=db,
        owner_id=owner.id,
    )
    return flags


def test_ground_cap_flags_only_ground_transport_over_cap_at_presubmit(
    db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report, ride = _ground_report(db_session, org_a, owner)

    flags = _presubmit(db_session, report, owner)

    assert len(flags) == 1, [fl.to_dict() for fl in flags]
    (flag,) = flags
    assert flag.flag_type == "policy_violation"
    assert flag.entry_id == str(ride.id)
    assert flag.severity == "blocking" and flag.justifiable is True
    assert flag.requires_approver == "manager"

    ride.justification = "Airport transfer at 2am; no transit running."
    db_session.flush()
    (justified,) = _presubmit(db_session, report, owner)
    assert justified.severity == "advisory"
    assert justified.flag_type == "policy_violation"
    assert justified.justification == ride.justification


def test_justified_ground_cap_breach_is_still_escalated_at_approval(
    db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report, ride = _ground_report(db_session, org_a, owner)
    ride.justification = "Airport transfer at 2am; no transit running."
    db_session.flush()
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )

    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)

    assert risk.is_policy_compliant is False
    assert decide_route(risk) == "escalate"
    ground = [fl for fl in risk.flags_snapshot if fl.get("rule_type") == "amount_cap"]
    assert len(ground) == 1
    assert ground[0]["justification"] == ride.justification
    assert not any(fl.get("flag_type") == "not_evaluated" for fl in risk.flags_snapshot)
