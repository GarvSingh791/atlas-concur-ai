"""C7R-04 — AI rule authoring end to end: the clause ref is a REAL corpus clause or null.

Staging re-acceptance: the LLM copied the user's arbitrary prefix ("C7R") into
source_clause_ref, and the drafted rule + its ``policy_rule.drafted`` audit row
carried it. Real router + orchestrator + PolicyAgent, scripted LLM, rollback DB.

Pinned:
  * a ref that is not one of the organization's corpus clause ids -> null on the
    created rule, the 201 response and the audit row;
  * a real corpus clause id (any casing) -> kept, in the corpus spelling;
  * another organization's corpus never validates a ref (tenant isolation);
  * still 201 + exactly one ``policy_rule.drafted`` row (provenance ai_generated);
  * the readable-title helper names the drafted rule (``draft_rule_title``).
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
from ai_agents.agents.policy_authoring import draft_rule_title
from backend.app.modules.ai import runtime
from backend.app.modules.ai.runtime import get_orchestrator
from database.models.audit_log import AuditLog
from database.models.policy_embedding import PolicyEmbedding
from database.models.policy_rule import PolicyRule
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

pytestmark = pytest.mark.db

API = "/api/v1"
_TITLE = "C7R Hotel cap for Tier 2 cities $275 per night"


def _rule(ref: str | None) -> dict[str, Any]:
    return {
        "rule_type": "amount_cap",
        "applies_to": "hotel",
        "definition": {
            "currency": "USD",
            "per": "night",
            "tiers": [{"tier": "tier_2", "cap": 275}],
            "exception_approver": "manager",
        },
        "source_clause_ref": ref,
    }


class _ScriptedLlm(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.append(messages[-1].content)
        text = self._responses[(len(self.prompts) - 1) % len(self._responses)]
        return LlmResponse(text=text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1))

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _index_clause(db: Session, org: Any, clause_ref: str) -> None:
    from services.embeddings_provider.local import embed

    doc = f.make_policy(db, org=org, is_active=False, name=f"Corpus {uuid.uuid4().hex[:6]}")
    text = f"{clause_ref} Hotel nightly caps by city tier."
    db.add(
        PolicyEmbedding(
            policy_id=doc.id,
            chunk_text=text,
            clause_ref=clause_ref,
            ordinal=0,
            embedding=embed(text),
        )
    )
    db.flush()


@pytest.fixture()
def author(app: Any, local_ai: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _install(ref: str | None) -> _ScriptedLlm:
        llm = _ScriptedLlm(json.dumps(_rule(ref)), "FAITHFUL\nCaptures the cap.")
        monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
        runtime.build_orchestrator.cache_clear()
        app.dependency_overrides.pop(get_orchestrator, None)
        return llm

    yield _install
    runtime.build_orchestrator.cache_clear()


def _drafted(db: Session, org_id: uuid.UUID) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(
                AuditLog.organization_id == org_id, AuditLog.action == "policy_rule.drafted"
            )
        ).all()
    )


@pytest.mark.parametrize(
    ("llm_ref", "expected"),
    [
        ("C7R", None),  # the staging defect: the user's prefix echoed by the model
        ("POL-001", None),  # well-formed but not a corpus clause
        ("hotel-cap", "HOTEL-CAP"),  # real clause, canonical corpus spelling
        (None, None),
    ],
)
def test_drafted_rule_clause_ref_is_a_real_corpus_clause_or_null(
    author: Any,
    finance_client: Any,
    db_session: Session,
    org_a: Any,
    llm_ref: str | None,
    expected: str | None,
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    _index_clause(db_session, org_a, "HOTEL-CAP")
    llm = author(llm_ref)

    r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _TITLE})

    assert r.status_code == 201, r.text
    assert _TITLE in llm.prompts[0]
    body = r.json()
    assert body["status"] == "draft"
    assert body["source_clause_ref"] == expected
    # C7-04 (cross-review): the API title is the readable one, not the raw rule_type.
    assert body["title"] == "Hotel cap — Tier 2 cities $275/night"
    rule = db_session.get(PolicyRule, uuid.UUID(body["id"]))
    assert rule is not None and rule.source_clause_ref == expected
    audits = _drafted(db_session, org_a.id)
    assert [str(a.entity_id) for a in audits] == [body["id"]]
    assert audits[0].after_state is not None
    assert audits[0].after_state["source_clause_ref"] == expected
    assert audits[0].after_state["provenance"] == "ai_generated"
    assert draft_rule_title(rule.rule_type, rule.applies_to, rule.definition) == (
        "Hotel cap — Tier 2 cities $275/night"
    )


def test_another_orgs_corpus_never_validates_a_ref(
    author: Any, finance_client: Any, db_session: Session, org_a: Any, org_b: Any
) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    _index_clause(db_session, org_b, "C7R")  # exists ONLY in the other tenant's corpus
    author("C7R")

    r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _TITLE})

    assert r.status_code == 201, r.text
    assert r.json()["source_clause_ref"] is None


def test_corpus_lookup_error_fails_closed_without_aborting_the_transaction(
    db_session: Session, org_a: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round: a DB error inside the corpus lookup degrades to "no clause
    ids" AND leaves the caller's transaction usable (SAVEPOINT), so the draft rule
    can still be flushed. Without the savepoint Postgres aborts the transaction
    and the next statement raises InFailedSqlTransaction."""
    from ai_agents.agents.policy_authoring import load_corpus_clause_refs

    policy = f.make_policy(db_session, org=org_a, is_active=True)
    _index_clause(db_session, org_a, "HOTEL-CAP")
    real_scalars = db_session.scalars

    def failing_scalars(*args: Any, **kwargs: Any) -> Any:
        # A genuine server-side error (division by zero) inside the lookup.
        db_session.execute(sa.text("SELECT 1/0"))
        return real_scalars(*args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(db_session, "scalars", failing_scalars)
    assert load_corpus_clause_refs(db_session, policy_id=policy.id) == frozenset()
    monkeypatch.setattr(db_session, "scalars", real_scalars)

    # The transaction is still alive: the lookup works again and writes flush.
    assert db_session.execute(sa.text("SELECT 1")).scalar_one() == 1
    assert load_corpus_clause_refs(db_session, policy_id=policy.id) == frozenset({"HOTEL-CAP"})
