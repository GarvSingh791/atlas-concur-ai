"""Tier-2b DB AI-eval gate — RAG retrieval + M-A3 DB corroboration (P7-T3b).

``@pytest.mark.db`` — needs a live Postgres + pgvector (the new ``eval-db`` CI
job; self-skips when Postgres is unreachable, so it never runs in the bare
hermetic CI job).  This is the only tier that exercises the parts that genuinely
need the database: the org-scoping JOIN, the pgvector ANN search, the real
org-scoped rule-loader, and the persisted ``ai_decision_logs`` read.

Three sections (each its own focused test), all driven over a TWO-ORG seed so
cross-tenant isolation is a real probe, not an assumption:

  1. RAG recall — seed org_a a Policy + the 8 policy clause embeddings (direct
     ``PolicyEmbedding`` rows built with ``LocalEmbeddingsProvider`` over
     ``database/seeds/policy_content.CLAUSES`` — the SAME embedder + chunk_text
     ``seed_all`` uses, so the dataset queries that lift tokens from CLAUSES
     ground exactly as in production).  For every grounded rag case run the REAL
     ``GroundedRetriever().retrieve`` and score recall@5 / precision@5 / hit@1
     against ``gold_clause_refs``; for every silent case assert
     ``GroundingStatus.silent`` (no chunk cleared the 0.20 gate).

  2. ORG-SCOPING (hard) — org_b is seeded its OWN Policy + the same 8 embeddings.
     A retrieval scoped to org_a must return ZERO chunks whose embedding belongs
     to org_b's policy (``policy_embeddings`` carries no organization_id; the
     scoping lives entirely in the JOIN to ``policies`` at
     ai_agents/tools/policy_retrieval.py:107-109).  A cross-tenant chunk is both
     a data leak AND a correctness failure — gated at exactly 0.

  3. M-A3 DB corroboration + PERSISTED DECISION LOG — seed org_a the 7 active
     policy_rules (from ``evals/datasets/policy_rules.json``) and run the REAL
     ``PolicyEvalTool().evaluate`` through the org-scoped JOIN ``_load_active_rules``
     for every m_a3 case; ``classify()`` must equal the gold label (this confirms
     the hermetic Tier-1 result holds against a real DB load, not just patched
     in-memory rules).  Then drive ``PolicyEvalHandler.handle`` so
     ``write_decision_log`` persists a real row, query ``ai_decision_logs`` back,
     and assert the DECISION read FROM THE PERSISTED ROW (route / confidence /
     output_summary) — satisfying the brief's "compute from ai_decision_logs".

Determinism: ``LocalEmbeddingsProvider`` is a salted-``hash()`` feature embedder,
so absolute cosine similarities are stable only WITHIN one interpreter — the seed
and the query run in the SAME process here (one db_session), and the CI run
command pins ``PYTHONHASHSEED=0``, so the grounded/silent gold labels hold.

Side effects: merges the ``rag`` + ``m_a3_db`` sections into
``evals/reports/metrics.json`` (CI artifact), preserving the M-A1/M-A2/M-A3
sections written by the other entrypoints.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as factories
from ai_agents.agents.policy_eval_handler import PolicyEvalHandler
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.policy_eval import PolicyEvalTool
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.identity import Organization, User
from database.models.policy import Policy
from database.models.policy_embedding import PolicyEmbedding
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import CLAUSES, DOCUMENT_TEXT, POLICY_NAME
from evals import thresholds
from evals.harness import metrics, runners, snapshot
from evals.harness.loaders import MA3Case, PolicyRuleDef, RagCase
from evals.harness.policy_decision import LABELS, classify, is_flag
from services.embeddings_provider.local import LocalEmbeddingsProvider

pytestmark = pytest.mark.db

# Production retrieval defaults (mirrored for assertions; NOT a redefinition —
# the tool reads these from Settings).  k for the @k metrics; gate for "silent".
_RAG_K = thresholds.RAG_K  # 5
_SILENT_GATE = 0.20  # ai_agents/rag/retrieval.py RETRIEVAL_SIMILARITY_THRESHOLD

# Committed regression snapshot of the per-grounded-case retrieved clause_refs.
_SNAP_RAG = "db_rag_retrieved_refs"

# Module-level caches so the (DB-bound) seed + score happen exactly ONCE even
# though several focused test functions consume them.  Both sections write plain
# JSON-able structures (no live-ORM objects) so the cache outlives the
# rolled-back db_session that produced it.
_RAG_CACHE: dict[str, Any] | None = None
_MA3_CACHE: dict[str, Any] | None = None


# ===========================================================================
# Two-org seed helpers (harness-owned; never edits database/seeds)
# ===========================================================================


def _seed_policy_with_embeddings(
    session: Session,
    org: Organization,
    vectors: list[list[float]],
) -> Policy:
    """Seed one org a Policy + the 8 clause embeddings.

    Mirrors ``database/seeds/seed_all._seed_policy`` exactly: direct
    ``PolicyEmbedding`` rows whose ``chunk_text`` / ``clause_ref`` are the real
    ``policy_content.CLAUSES`` and whose vector is ``LocalEmbeddingsProvider``
    over that chunk_text.  ``vectors`` is precomputed ONCE and shared across both
    orgs so org_a and org_b carry byte-identical embeddings — which makes the
    org-scoping test honest: the ONLY thing keeping org_b's chunks out of org_a's
    results is the JOIN, not a vector difference.
    """
    policy = Policy(
        organization_id=org.id,
        name=POLICY_NAME,
        version=1,
        effective_from=date(2026, 1, 1),
        document_text=DOCUMENT_TEXT,
        is_active=True,
    )
    session.add(policy)
    session.flush()  # populate policy.id
    for clause, vec in zip(CLAUSES, vectors, strict=True):
        session.add(
            PolicyEmbedding(
                policy_id=policy.id,
                chunk_text=clause["chunk_text"],
                clause_ref=clause["clause_ref"],
                embedding=vec,
            )
        )
    session.flush()
    return policy


def _seed_active_rules(session: Session, policy: Policy, defs: list[PolicyRuleDef]) -> None:
    """Seed the 7 active policy_rules under ``policy`` from the rule defs.

    Uses the real ``PolicyRule`` rows (status=active) so the tool's org-scoping
    JOIN ``_load_active_rules`` (policy_eval.py:205) loads them exactly as it
    would in production.
    """
    for d in defs:
        session.add(
            PolicyRule(
                policy_id=policy.id,
                rule_type=PolicyRuleType(d.rule_type),
                applies_to=PolicyAppliesTo(d.applies_to),
                definition=d.definition,
                source_clause_ref=d.source_clause_ref,
                status=PolicyRuleStatus.active,
            )
        )
    session.flush()


# ===========================================================================
# Section 1 + 2: RAG retrieval + org-scoping (one shared two-org seed)
# ===========================================================================


def _score_rag(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    employee: User,
    rag_cases: list[RagCase],
) -> dict[str, Any]:
    """Seed org_a AND org_b, run GroundedRetriever for org_a over every rag case.

    Returns plain JSON-able structures (per-case retrieved clause_refs, the @k
    aggregates, the silent results, and the cross-tenant chunk count) so the
    result survives the db_session teardown for the module cache.
    """
    embedder = LocalEmbeddingsProvider()
    # Embed the 8 clauses ONCE; share the identical vectors across both orgs.
    vectors = embedder.embed_texts([c["chunk_text"] for c in CLAUSES])

    _seed_policy_with_embeddings(db_session, org_a, vectors)
    org_b_policy = _seed_policy_with_embeddings(db_session, org_b, vectors)

    # The exact set of embedding ids that belong to org_b (the leak target).
    org_b_embedding_ids: set[str] = {
        str(row[0])
        for row in db_session.execute(
            sa.text("SELECT id::text FROM policy_embeddings WHERE policy_id = :pid"),
            {"pid": str(org_b_policy.id)},
        )
    }
    assert len(org_b_embedding_ids) == len(CLAUSES), "org_b seed did not insert all clause rows"

    ctx_a = AgentContext(
        organization_id=org_a.id,
        granted_permission_keys=frozenset(),
        user_id=employee.id,
    )

    # GroundedRetriever over a PolicyRetrievalTool with the SAME embedder that
    # seeded — explicit injection so the result never depends on factory drift.
    gr = GroundedRetriever(retriever=PolicyRetrievalTool(embedder=embedder))
    # A raw tool (top_k wide) for the org-scoping probe: we want to inspect EVERY
    # chunk the org-scoped SQL would surface, not just the threshold-passing prefix.
    raw_tool = PolicyRetrievalTool(embedder=embedder)

    grounded_cases = [c for c in rag_cases if c.expect == "grounded"]
    silent_cases = [c for c in rag_cases if c.expect == "silent"]

    # --- Grounded: recall@k / precision@k / hit@1 vs gold_clause_refs ----------
    retrieval_pairs: list[tuple[list[str], list[str]]] = []
    per_case_refs: dict[str, list[str]] = {}
    # Per-case regression snapshot: the SORTED SET of GOLD clauses this query
    # actually grounded (retrieved set ∩ gold).  The @k metrics above are
    # order-aware; the snapshot guards WHICH gold clauses ground.  Restricting to
    # the gold intersection makes it robust to the local salted-hash embedder's
    # incidental near-threshold FILLER reordering/membership across PYTHONHASHSEED
    # values (gold clauses sit well above the 0.20 gate; filler hovers at it),
    # while still tripping if a gold clause stops grounding (a real regression).
    per_case_gold_hits: dict[str, list[str]] = {}
    grounded_status_failures: list[tuple[str, str | None]] = []
    hit_at_1_hits = 0
    for case in grounded_cases:
        result = gr.retrieve(case.query, context=ctx_a, session=db_session)
        refs = [c.clause_ref for c in result.clauses if c.clause_ref is not None]
        per_case_refs[case.id] = refs
        per_case_gold_hits[case.id] = sorted(set(refs) & set(case.gold_clause_refs))
        retrieval_pairs.append((refs, case.gold_clause_refs))
        if result.status != GroundingStatus.grounded:
            grounded_status_failures.append((case.id, result.reason))
        if refs and refs[0] in set(case.gold_clause_refs):
            hit_at_1_hits += 1

    agg = metrics.aggregate_retrieval(retrieval_pairs, _RAG_K)
    hit_at_1 = hit_at_1_hits / len(grounded_cases) if grounded_cases else 0.0

    # --- Silent: every silent case must NOT ground (no chunk >= the gate) ------
    silent_failures: list[tuple[str, list[str], float | None]] = []
    for case in silent_cases:
        result = gr.retrieve(case.query, context=ctx_a, session=db_session)
        if result.status != GroundingStatus.silent:
            silent_failures.append(
                (case.id, [c.clause_ref or "" for c in result.clauses], result.confidence)
            )

    # --- Org-scoping (hard): retrieve for org_a, count org_b chunks ------------
    # Probe with a WIDE top_k so even a low-ranked cross-tenant chunk surfaces; we
    # use the grounded queries (the ones with real token overlap) so the ANN
    # search is actually exercised, not a no-op.
    cross_tenant_hits: list[tuple[str, str]] = []
    for case in grounded_cases:
        rr = raw_tool.retrieve(
            RetrievalIntent(query=case.query, top_k=2 * len(CLAUSES)),
            context=ctx_a,
            session=db_session,
        )
        for chunk in rr.chunks:
            if chunk.embedding_id in org_b_embedding_ids:
                cross_tenant_hits.append((case.id, chunk.embedding_id))

    return {
        "n_total": len(rag_cases),
        "n_grounded": len(grounded_cases),
        "n_silent": len(silent_cases),
        "agg": agg,
        "hit_at_1": hit_at_1,
        "per_case_refs": per_case_refs,
        "per_case_gold_hits": per_case_gold_hits,
        "grounded_status_failures": grounded_status_failures,
        "silent_failures": silent_failures,
        "cross_tenant_hits": cross_tenant_hits,
        "org_b_embedding_count": len(org_b_embedding_ids),
    }


@pytest.fixture()
def rag_scored(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    rag_cases: list[RagCase],
) -> dict[str, Any]:
    """Seed both orgs + score every rag case ONCE; cache for the module.

    Function-scoped because the seed genuinely needs function-scoped
    ``db_session`` / ``org_a`` / ``org_b`` / ``users_a``; the plain-dict result is
    memoised at module level so the focused tests reuse it without re-seeding.
    """
    global _RAG_CACHE
    if _RAG_CACHE is None:
        _RAG_CACHE = _score_rag(db_session, org_a, org_b, users_a["employee"], rag_cases)
    return _RAG_CACHE


def test_rag_dataset_is_substantial(rag_scored: dict[str, Any]) -> None:
    """Sanity: enough grounded + silent cases that the floors mean something."""
    assert rag_scored["n_grounded"] >= 15, (
        f"too few grounded rag cases to be a meaningful recall gate "
        f"(n={rag_scored['n_grounded']})"
    )
    assert (
        rag_scored["n_silent"] >= 3
    ), f"too few silent rag cases to probe the silent gate (n={rag_scored['n_silent']})"


def test_rag_recall_at_k_floor(rag_scored: dict[str, Any]) -> None:
    """Mean Recall@5 over grounded cases >= floor.

    Recall@5 = fraction of each case's gold clause refs that appear in the top-5
    retrieved chunks.  A retrieval regression (wrong ranking, a dropped clause, a
    broken embedder) lowers this; a swapped gold clause ref in the dataset also
    lowers it — both are caught.
    """
    # Every grounded case must actually ground, or the recall number is a lie.
    assert not rag_scored[
        "grounded_status_failures"
    ], f"grounded rag cases unexpectedly went silent: {rag_scored['grounded_status_failures']}"
    recall = rag_scored["agg"]["recall_at_k"]
    print(
        f"\nRAG recall@{_RAG_K} = {recall:.4f} "
        f"(floor {thresholds.RAG_RECALL_AT_K_FLOOR}, n={rag_scored['agg']['n']})"
    )
    assert recall >= thresholds.RAG_RECALL_AT_K_FLOOR, (
        f"RAG recall@{_RAG_K} {recall:.4f} < floor {thresholds.RAG_RECALL_AT_K_FLOOR}: "
        f"retrieval missed gold clause refs — a grounding regression."
    )


def test_rag_hit_at_1_floor(rag_scored: dict[str, Any]) -> None:
    """Hit@1 (the single best chunk is a gold clause ref) over grounded cases."""
    hit1 = rag_scored["hit_at_1"]
    print(
        f"RAG hit@1 = {hit1:.4f} "
        f"(floor {thresholds.RAG_HIT_AT_1_FLOOR}, n={rag_scored['n_grounded']})"
    )
    assert hit1 >= thresholds.RAG_HIT_AT_1_FLOOR, (
        f"RAG hit@1 {hit1:.4f} < floor {thresholds.RAG_HIT_AT_1_FLOOR}: "
        f"the top-ranked chunk is too often NOT a gold clause — a ranking regression."
    )


def test_rag_silent_queries_stay_silent(rag_scored: dict[str, Any]) -> None:
    """Off-topic queries must return GroundingStatus.silent (no chunk >= the gate).

    A silent case that grounds is a false grounding (the system asserting a
    citation it should not).  Asserted exactly: zero silent cases may leak.
    """
    failures = rag_scored["silent_failures"]
    assert not failures, (
        f"silent rag cases unexpectedly grounded (max chunk cleared the {_SILENT_GATE} gate): "
        f"{failures}"
    )


def test_rag_org_scoping_zero_cross_tenant(rag_scored: dict[str, Any]) -> None:
    """HARD: a retrieval scoped to org_a returns ZERO chunks belonging to org_b.

    Both orgs carry byte-identical embeddings, so the ONLY thing keeping org_b's
    chunks out of org_a's results is the org-scoping JOIN
    (policy_retrieval.py:107-109).  ``policy_embeddings`` has no organization_id,
    so a cross-tenant chunk is a data leak AND a correctness failure.
    """
    assert rag_scored["org_b_embedding_count"] == len(
        CLAUSES
    ), "org_b must be seeded its own embeddings for this to be a real probe"
    cross = rag_scored["cross_tenant_hits"]
    print(
        f"\nRAG org-scoping: {len(cross)} cross-tenant chunk(s) "
        f"(max allowed {thresholds.RAG_MAX_CROSS_TENANT_CHUNKS}); "
        f"org_b had {rag_scored['org_b_embedding_count']} embeddings"
    )
    assert len(cross) <= thresholds.RAG_MAX_CROSS_TENANT_CHUNKS, (
        f"CROSS-TENANT LEAK: org_a retrieval returned {len(cross)} chunk(s) owned by org_b "
        f"(the org-scoping JOIN failed): {cross[:10]}"
    )


def test_rag_snapshot(rag_scored: dict[str, Any]) -> None:
    """Regression snapshot of the GOLD clauses each grounded query actually grounds.

    Guards the PRODUCED retrieval output (which of a case's gold clauses cleared the
    threshold), not gold itself: a re-tuned embedder, a different chunking, or a
    threshold shift that drops a gold clause surfaces as a reviewable diff and
    requires a deliberate snapshot update (ATLAS_EVAL_UPDATE=1), never an auto-pass.

    Stored as the SORTED gold-intersection set (not the full ranked top-k):
    ranking quality is already gated by the recall@k / hit@1 floors, and the
    near-threshold non-gold FILLER is incidental to the local salted-hash
    embedder's bucketing (it reorders/drops across PYTHONHASHSEED values).  The
    gold clauses sit well above the 0.20 gate, so this snapshot is stable across
    seeds yet still trips the moment a gold clause stops grounding.
    """
    snapshot.assert_snapshot(_SNAP_RAG, rag_scored["per_case_gold_hits"])


# ===========================================================================
# Section 3: M-A3 DB corroboration + persisted decision-log read
# ===========================================================================


def _score_m_a3_db(
    db_session: Session,
    org_a: Organization,
    employee: User,
    m_a3_cases: list[MA3Case],
    rule_defs: list[PolicyRuleDef],
) -> dict[str, Any]:
    """Seed org_a's 7 active rules; run REAL evaluate() over every m_a3 case.

    Uses the org-scoped JOIN ``_load_active_rules`` (the production DB path), then
    ``classify()`` -> compares to gold.  Returns plain (gold, pred) pairs + the
    persisted-log read so the result survives the rolled-back session.
    """
    policy = factories.make_policy(db_session, org=org_a, name="Atlas Eval Policy")
    _seed_active_rules(db_session, policy, rule_defs)

    ctx = AgentContext(
        organization_id=org_a.id,
        granted_permission_keys=frozenset(),
        user_id=employee.id,  # a REAL user — ai_decision_logs.user_id is an FK
    )

    tool = PolicyEvalTool()
    pairs: list[tuple[str, str]] = []
    mismatches: list[tuple[str, str, str]] = []
    for case in m_a3_cases:
        item = runners.build_eval_item(case.item)
        verdict = tool.evaluate(item, context=ctx, session=db_session)
        pred = classify(verdict)
        pairs.append((case.gold_label, pred))
        if pred != case.gold_label:
            mismatches.append((case.id, case.gold_label, pred))

    # --- Persisted decision-log read --------------------------------------
    # Drive the REAL PolicyEvalHandler for a representative pair (one definite
    # violation + one definite in_policy) so write_decision_log persists rows,
    # then read the DECISION back FROM ai_decision_logs (not the return value).
    viol_case = next(c for c in m_a3_cases if c.gold_label == "violation")
    inpol_case = next(c for c in m_a3_cases if c.gold_label == "in_policy")
    handler = PolicyEvalHandler()
    # (case_id, gold_label, expected in_policy, ref_id) — ref_id is the STABLE
    # per-case discriminator the handler persists in input_summary (it is NOT one
    # of the asserted fields), so the persisted rows can be matched back to their
    # source case independently of row ordering.
    handled: list[tuple[str, str, bool, str]] = []
    for case, expected_in_policy in ((viol_case, False), (inpol_case, True)):
        ref_id = case.item.get("ref_id")
        assert ref_id is not None, (
            f"{case.id}: case has no item.ref_id — needed as the stable per-row "
            f"discriminator for the persisted-log read"
        )
        task = Task(
            context_type="policy_qa",
            payload=dict(case.item),
            task_type="policy_evaluate",
        )
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.deterministic,
            task_type="policy_evaluate",
            confidence=1.0,
            rationale="ai-eval Tier-2b persisted-log probe",
        )
        result = handler.handle(task, decision, context=ctx, session=db_session)
        assert result.status == AgentResultStatus.ok, f"handler failed for {case.id}"
        handled.append((case.id, case.gold_label, expected_in_policy, str(ref_id)))

    # Read the persisted rows back — the source of truth for this assertion.  The
    # two rows are written in the SAME transaction, so transaction_timestamp() gives
    # them byte-identical created_at; ORDER BY created_at is therefore a tie with
    # undefined order.  We KEY each row by its input_summary.ref_id (the handler
    # persists {item_type, ref_id} at policy_eval_handler.py:72) instead of relying
    # on row order, so the match to the source case is stable.
    rows = db_session.execute(
        sa.text(
            "SELECT agent::text AS agent, task_type, route::text AS route, "
            "confidence, input_summary, output_summary "
            "FROM ai_decision_logs WHERE organization_id = :org"
        ),
        {"org": str(org_a.id)},
    ).fetchall()
    persisted = [
        {
            "agent": r.agent,
            "task_type": r.task_type,
            "route": r.route,
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "input_summary": r.input_summary,
            "output_summary": r.output_summary,
        }
        for r in rows
    ]

    return {
        "n_cases": len(m_a3_cases),
        "pairs": pairs,
        "mismatches": mismatches,
        "handled": handled,
        "persisted_rows": persisted,
    }


@pytest.fixture()
def m_a3_db_scored(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    m_a3_cases: list[MA3Case],
    policy_rule_defs: list[PolicyRuleDef],
) -> dict[str, Any]:
    """Seed org_a's rules + score every m_a3 case through the real DB load ONCE."""
    global _MA3_CACHE
    if _MA3_CACHE is None:
        _MA3_CACHE = _score_m_a3_db(
            db_session, org_a, users_a["employee"], m_a3_cases, policy_rule_defs
        )
    return _MA3_CACHE


def test_m_a3_db_corroborates_gold(m_a3_db_scored: dict[str, Any]) -> None:
    """The REAL org-scoped DB load reproduces every gold label exactly.

    This is the corroboration the hermetic Tier-1 gate cannot give: Tier-1 patches
    ``_load_active_rules`` with in-memory rows, so a divergence between the patched
    rules and the seeded rules (or a bug in the org-scoping JOIN itself) would slip
    past it.  Here the rules are REAL DB rows loaded through the production JOIN.
    A single mismatch means either the dataset drifted from the seed rules or the
    DB load behaves differently from the in-memory load — both must be caught.
    """
    n = m_a3_db_scored["n_cases"]
    mismatches = m_a3_db_scored["mismatches"]
    print(
        f"\nM-A3 DB corroboration: {n - len(mismatches)}/{n} labels reproduced "
        f"via the real org-scoped JOIN load"
    )
    assert n >= 60, f"M-A3 dataset too small to be a meaningful DB gate (n={n})"
    assert not mismatches, (
        f"M-A3 DB load diverged from gold on {len(mismatches)} case(s) "
        f"(dataset/seed-rule drift or org-scoping JOIN bug): {mismatches}"
    )


def test_m_a3_db_violation_recall_hard_gate(m_a3_db_scored: dict[str, Any]) -> None:
    """HARD GATE (DB tier): every gold 'violation' is predicted 'violation'.

    The same no-silent-in-policy-false-pass invariant as Tier-1, re-asserted
    against the real DB load.  Violation-class recall must be EXACTLY 1.0; any
    value below BLOCKS the phase.
    """
    pairs: list[tuple[str, str]] = m_a3_db_scored["pairs"]
    prf = metrics.per_class_prf(pairs, LABELS)["violation"]
    print(
        f"M-A3 DB violation-class: recall={prf.recall:.4f} (HARD GATE == "
        f"{thresholds.MA3_VIOLATION_RECALL_HARD_GATE}) support={prf.support} fn={prf.fn}"
    )
    assert prf.support > 0, "no gold violations in the dataset — the hard gate is vacuous"
    assert prf.recall == thresholds.MA3_VIOLATION_RECALL_HARD_GATE, (
        f"VIOLATION-RECALL {prf.recall:.4f} != 1.0 over the real DB load: "
        f"{prf.fn} real violation(s) passed as in_policy/needs_review — BLOCKS the phase."
    )


def test_m_a3_db_flag_precision_floor(m_a3_db_scored: dict[str, Any]) -> None:
    """Flag-class precision (DB tier) >= floor — bounds false alarms.

    A 'flag' is a predicted label in {violation, needs_review}; it is correct iff
    the gold label is also a flag.  Re-asserted against the real DB load so the
    bound holds on the production path, not just the patched-rule path.
    """
    pairs: list[tuple[str, str]] = m_a3_db_scored["pairs"]
    flag_tp = sum(1 for g, p in pairs if is_flag(p) and is_flag(g))
    flag_fp = sum(1 for g, p in pairs if is_flag(p) and not is_flag(g))
    flag_predicted = flag_tp + flag_fp
    precision = flag_tp / flag_predicted if flag_predicted else 0.0
    print(
        f"M-A3 DB flag-class precision = {precision:.4f} "
        f"(floor {thresholds.MA3_FLAG_PRECISION_FLOOR}; tp={flag_tp} fp={flag_fp})"
    )
    assert flag_predicted > 0, "no flags predicted — flag-precision is vacuous"
    assert precision >= thresholds.MA3_FLAG_PRECISION_FLOOR, (
        f"flag-precision {precision:.4f} < floor {thresholds.MA3_FLAG_PRECISION_FLOOR}: "
        f"{flag_fp} in-policy item(s) wrongly flagged — the system cries wolf too often."
    )


def test_m_a3_persisted_decision_log(m_a3_db_scored: dict[str, Any]) -> None:
    """Read the DECISION back from the persisted ai_decision_logs rows.

    The brief's "compute the decision from ai_decision_logs" requirement: we drove
    the real handler (which calls write_decision_log) and now assert on the rows it
    persisted — NOT on the handler's return value.  Each row must carry the
    deterministic route, full confidence, and an output_summary whose in_policy
    matches the gold class of the item that produced it (violation -> False,
    in_policy -> True).

    Both rows are written in the SAME transaction, so created_at is byte-identical
    and ORDER BY created_at is an undefined-order tie.  We therefore match each
    persisted row to its source case by the STABLE per-case discriminator the
    handler writes into input_summary.ref_id (policy_eval_handler.py:72) — a field
    distinct from the route/confidence/output_summary.in_policy we assert on — so
    the assertion never depends on row ordering.
    """
    handled = m_a3_db_scored["handled"]
    rows = m_a3_db_scored["persisted_rows"]
    assert len(rows) == len(handled), (
        f"expected {len(handled)} persisted ai_decision_logs row(s), got {len(rows)} "
        f"(write_decision_log did not persist as expected)"
    )
    # Index the persisted rows by their input_summary.ref_id (order-independent).
    rows_by_ref: dict[str, dict[str, Any]] = {}
    for row in rows:
        in_sum = row["input_summary"]
        assert isinstance(in_sum, dict), (
            f"persisted input_summary is not an object: {in_sum!r} "
            f"(cannot key the row to its source case)"
        )
        ref = in_sum.get("ref_id")
        assert ref is not None, (
            f"persisted input_summary missing ref_id: {in_sum!r} "
            f"(handler must persist the per-case discriminator)"
        )
        assert ref not in rows_by_ref, (
            f"two persisted rows share ref_id {ref!r} — discriminator is not unique, "
            f"cannot match rows to cases unambiguously"
        )
        rows_by_ref[str(ref)] = row

    for case_id, gold_label, expected_in_policy, ref_id in handled:
        assert ref_id in rows_by_ref, (
            f"{case_id}: no persisted ai_decision_logs row with ref_id {ref_id!r} "
            f"(write_decision_log did not persist this case)"
        )
        row = rows_by_ref[ref_id]
        assert (
            row["agent"] == AiAgent.policy.value
        ), f"{case_id}: persisted agent {row['agent']!r} != 'policy'"
        assert (
            row["task_type"] == "policy_evaluate"
        ), f"{case_id}: persisted task_type {row['task_type']!r} != 'policy_evaluate'"
        assert (
            row["route"] == AiRoute.deterministic.value
        ), f"{case_id}: persisted route {row['route']!r} != 'deterministic'"
        assert (
            row["confidence"] == 1.0
        ), f"{case_id}: persisted confidence {row['confidence']!r} != 1.0"
        assert row["output_summary"] is not None, f"{case_id}: persisted output_summary is NULL"
        # THE DECISION, read from the persisted row, must match the gold class.
        assert row["output_summary"].get("in_policy") is expected_in_policy, (
            f"{case_id} (gold {gold_label}): persisted decision in_policy="
            f"{row['output_summary'].get('in_policy')!r} != expected {expected_in_policy!r}"
        )


# ===========================================================================
# metrics.json artifact — MERGE the rag + m_a3_db sections (keep the rest)
# ===========================================================================


def test_emit_metrics_report(rag_scored: dict[str, Any], m_a3_db_scored: dict[str, Any]) -> None:
    """Write/merge the rag + m_a3_db sections into evals/reports/metrics.json.

    Preserves the m_a1/m_a2/m_a3 sections the other entrypoints wrote — this is a
    MERGE, not an overwrite, so a single ``metrics.json`` artifact carries all
    tiers when the full eval suite runs.
    """
    existing: dict[str, Any] = {}
    if metrics.REPORT_PATH.exists():
        existing = json.loads(metrics.REPORT_PATH.read_text(encoding="utf-8"))

    pairs: list[tuple[str, str]] = m_a3_db_scored["pairs"]
    per_class = metrics.per_class_prf(pairs, LABELS)
    matrix = metrics.confusion_matrix(pairs, LABELS)

    existing["rag"] = {
        "tier": "db",
        "dataset_size": rag_scored["n_total"],
        "n_grounded": rag_scored["n_grounded"],
        "n_silent": rag_scored["n_silent"],
        "recall_at_k": rag_scored["agg"]["recall_at_k"],
        "precision_at_k": rag_scored["agg"]["precision_at_k"],
        "hit_at_1": round(rag_scored["hit_at_1"], 6),
        "k": _RAG_K,
        "recall_at_k_floor": thresholds.RAG_RECALL_AT_K_FLOOR,
        "hit_at_1_floor": thresholds.RAG_HIT_AT_1_FLOOR,
        "org_scoping": {
            "cross_tenant_chunks": len(rag_scored["cross_tenant_hits"]),
            "max_allowed": thresholds.RAG_MAX_CROSS_TENANT_CHUNKS,
            "org_b_embedding_count": rag_scored["org_b_embedding_count"],
        },
        "silent_leaks": len(rag_scored["silent_failures"]),
    }
    existing["m_a3_db"] = {
        "tier": "db",
        "dataset_size": m_a3_db_scored["n_cases"],
        "note": "real org-scoped JOIN load (corroborates hermetic Tier-1)",
        "per_class": {cls: per_class[cls] for cls in LABELS},
        "confusion_matrix": matrix,
        "violation_recall": round(per_class["violation"].recall, 6),
        "violation_recall_hard_gate": thresholds.MA3_VIOLATION_RECALL_HARD_GATE,
        "mismatch_count": len(m_a3_db_scored["mismatches"]),
        "persisted_log_rows": len(m_a3_db_scored["persisted_rows"]),
    }

    written = metrics.write_report(existing)
    assert written.exists()
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert reloaded["rag"]["dataset_size"] == rag_scored["n_total"]
    assert reloaded["m_a3_db"]["dataset_size"] == m_a3_db_scored["n_cases"]
    # The merge must not have dropped sibling sections written by other tiers.
    for sibling in ("m_a1", "m_a2", "m_a3"):
        if sibling in existing:
            assert sibling in reloaded, f"merge dropped the {sibling!r} section"
    print(f"\nWrote metrics report: {written}")
