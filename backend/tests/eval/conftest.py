"""Shared fixtures for the AI-eval entrypoints (P7-T3b).

These fixtures load the versioned golden datasets via ``evals.harness.loaders``
and build the in-memory ``PolicyRule`` list from ``evals/datasets/policy_rules.json``
(``status=active``) for the hermetic M-A3 gate.

REUSE, not redefine: the parent ``backend/tests/conftest.py`` owns ``db_session``,
``local_ai``, ``app``, the org/user topology, and the autouse session-scoped
``_schema`` fixture.  This conftest adds ONLY dataset/rule fixtures and a
hermetic ``AgentContext``.  None of the fixtures here touch the database — they
are pure dataset loads, so they are safe to use in the hermetic CI job where no
Postgres service exists.

Scope: dataset + rule fixtures are ``session``-scoped (the JSONL files are
immutable for the run, so parse once).  The context fixture is function-scoped
(cheap, and keeps each test's principal independent).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ai_agents.context import AgentContext
from database.models.policy_rule import PolicyRule
from evals.harness import loaders, runners
from evals.harness.loaders import (
    CorpusRagCase,
    MA1Case,
    MA2Case,
    MA3Case,
    PolicyRuleDef,
    RagCase,
)

# evals/ lives at the repo root: backend/tests/eval/conftest.py -> parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATASETS = _REPO_ROOT / "evals" / "datasets"


@pytest.fixture(scope="session")
def m_a1_cases() -> list[MA1Case]:
    """All M-A1 receipt-extraction golden cases (parsed + validated once).

    Pure dataset load (no DB) — safe to request from the session scope even
    though the Tier-2 entrypoint that consumes it is ``@pytest.mark.db``.
    """
    cases = loaders.load_m_a1(_DATASETS / "m_a1_extraction.jsonl")
    assert cases, "m_a1_extraction.jsonl loaded zero cases"
    return cases


@pytest.fixture(scope="session")
def m_a2_cases() -> list[MA2Case]:
    """All M-A2 smart-match golden cases (parsed + validated once)."""
    cases = loaders.load_m_a2(_DATASETS / "m_a2_match.jsonl")
    assert cases, "m_a2_match.jsonl loaded zero cases"
    return cases


@pytest.fixture(scope="session")
def m_a3_cases() -> list[MA3Case]:
    """All M-A3 policy-decision golden cases (parsed + validated once)."""
    cases = loaders.load_m_a3(_DATASETS / "m_a3_policy.jsonl")
    assert cases, "m_a3_policy.jsonl loaded zero cases"
    return cases


@pytest.fixture(scope="session")
def rag_cases() -> list[RagCase]:
    """All RAG retrieval golden cases (parsed + validated once).

    Pure dataset load (no DB) — consumed by the Tier-2b ``@pytest.mark.db``
    retrieval entrypoint, which seeds the embeddings and runs the real retriever.
    """
    cases = loaders.load_rag(_DATASETS / "rag_queries.jsonl")
    assert cases, "rag_queries.jsonl loaded zero cases"
    return cases


@pytest.fixture(scope="session")
def rag_corpus_cases() -> list[CorpusRagCase]:
    """All multi-document RAG-corpus gold cases (R0-T5), parsed + validated once.

    Pure dataset load (no DB).  Consumed by the hermetic dataset-integrity test and
    by the Tier-2b ``@pytest.mark.db`` corpus-baseline entrypoint.
    """
    cases = loaders.load_rag_corpus(_DATASETS / "rag_corpus_queries.jsonl")
    assert cases, "rag_corpus_queries.jsonl loaded zero cases"
    return cases


@pytest.fixture(scope="session")
def policy_rule_defs() -> list[PolicyRuleDef]:
    """The policy rule definitions mirroring ``policy_content.py`` RULES."""
    defs = loaders.load_policy_rules(_DATASETS / "policy_rules.json")
    assert defs, "policy_rules.json loaded zero rules"
    return defs


@pytest.fixture(scope="session")
def in_memory_policy_rules(policy_rule_defs: list[PolicyRuleDef]) -> list[PolicyRule]:
    """Unsaved, ``status=active`` ``PolicyRule`` rows for the hermetic M-A3 gate.

    These patch ``PolicyEvalTool._load_active_rules`` so the REAL per-rule logic
    runs with no DB and no org-scoping JOIN (the JOIN itself is exercised in the
    Tier-2 DB job).
    """
    return runners.build_policy_rules(policy_rule_defs)


@pytest.fixture()
def eval_context() -> AgentContext:
    """A minimal hermetic principal for the M-A3 tool calls.

    ``organization_id`` is arbitrary because the rule-loader is patched in the
    hermetic tier; ``granted_permission_keys`` is empty because PolicyEvalTool
    does not gate on permissions.
    """
    return AgentContext(
        organization_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        user_id=uuid.uuid4(),
    )
