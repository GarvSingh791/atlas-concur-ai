"""Manual (non-CI) PRODUCTION-embedder retrieval baseline over the Helios corpus (R0-T6).

``@pytest.mark.live`` + ``@pytest.mark.db`` -> SKIPPED by default; runs only with a REAL
embeddings key (``EMBEDDINGS_PROVIDER=openai`` + a valid ``EMBEDDINGS_API_KEY`` →
text-embedding-3-small, 1536-d, the SAME schema as local). Mirrors the eval-live pattern:
non-deterministic snapshot, NOT a CI gate.

Where the deterministic ``make eval`` baseline (`backend/tests/eval/test_rag_corpus_eval.py`)
measures the LOCAL feature-hash embedder, this re-embeds the 208 corpus units with the
PRODUCTION embedder and runs the SAME real ``PolicyRetrievalTool`` over the FULL gold set,
printing the same table + the trip-to-NY unit ranks and writing
``evals/reports/rag_corpus_production_baseline.json``. This is the reference R1's gains are
reported against for the live system, and it tells us what the live demo actually does for
"trip to New York".

Self-skips cleanly when the embedder is local / the key is a placeholder (a single probe
embed is attempted before any seeding). The full run makes ~1 batch + ~200 query embedding
calls — bounded, one-off.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from config.settings import get_settings
from database.models.identity import Organization
from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus.chunking import chunk_document
from evals.harness import metrics
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.interface import EmbeddingsProvider

pytestmark = pytest.mark.live

_METRIC_K = 10
_RETRIEVE_K = 25
_SILENT_GATE = 0.20


def _require_real_embedder() -> EmbeddingsProvider:
    """Skip cleanly unless a REAL embeddings provider is configured AND reachable."""
    get_settings.cache_clear()
    s = get_settings()
    if s.EMBEDDINGS_PROVIDER != "openai":
        pytest.skip("EMBEDDINGS_PROVIDER != openai — production-embedder baseline skipped.")
    try:
        provider = get_embeddings_provider()
        probe = provider.embed_texts(["hotel nightly rate cap tier one new york"])
    except Exception as exc:  # noqa: BLE001 — any provider/auth error → skip, never fail
        pytest.skip(f"real embeddings provider unavailable ({type(exc).__name__}) — skipped.")
    assert probe and len(probe[0]) == 1536, "real embedder did not return a 1536-d vector"
    return provider


def _seed_corpus_real(session: Session, org: Organization, provider: EmbeddingsProvider) -> None:
    """Seed the 208 corpus units under *org*, embedded with the PRODUCTION provider (batched)."""
    for doc in ALL_DOCUMENTS:
        policy = Policy(
            organization_id=org.id,
            name=doc.title,
            version=doc.version,
            effective_from=date.fromisoformat(doc.effective_from),
            document_text=doc.document_text,
            document_type=PolicyDocumentType(doc.document_type),
            is_active=True,
        )
        session.add(policy)
        session.flush()
        chunks = chunk_document(doc)
        vectors = provider.embed_texts([c.chunk_text for c in chunks])  # one batch call per doc
        for ch, vec in zip(chunks, vectors, strict=True):
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=ch.chunk_text,
                    clause_ref=ch.clause_ref,
                    section=ch.section,
                    parent_section_ref=ch.parent_section_ref,
                    ordinal=ch.ordinal,
                    embedding=vec,
                )
            )
    session.flush()


@pytest.mark.db
def test_corpus_production_embedder_baseline_live(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """Re-embed the corpus with text-embedding-3-small and record the retrieval baseline."""
    provider = _require_real_embedder()
    _seed_corpus_real(db_session, org_a, provider)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    tool = PolicyRetrievalTool(embedder=provider)  # query-time uses the SAME real provider

    ans_pairs: list[tuple[list[str], list[str]]] = []
    by_type: dict[str, list[tuple[list[str], list[str]]]] = {}
    risk_cov: list[tuple[float, bool]] = []
    coverage = 0
    abstained = 0
    n_ans = 0
    n_unans = 0
    ny: dict[str, object] = {}

    for case in rag_corpus_cases:
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=_RETRIEVE_K), context=ctx, session=db_session
        )
        refs = [c.clause_ref for c in r.chunks if c.clause_ref is not None]
        top1 = r.chunks[0].similarity if r.chunks else 0.0
        grounded = top1 >= _SILENT_GATE
        if case.answerable:
            n_ans += 1
            gold = case.expected_unit_ids
            ans_pairs.append((refs, gold))
            by_type.setdefault(case.type, []).append((refs, gold))
            hit = metrics.hit_at_k(refs, gold, _METRIC_K) >= 1.0
            coverage += 1 if grounded else 0
            risk_cov.append((top1, hit))
            if case.type == "trip_planning" and "new york" in case.query.lower() and not ny:
                wide = tool.retrieve(
                    RetrievalIntent(query=case.query, top_k=300), context=ctx, session=db_session
                )
                rank = {c.clause_ref: i + 1 for i, c in enumerate(wide.chunks)}
                ny = {
                    "query": case.query,
                    "ranks": {u: rank.get(u) for u in gold},
                    "top5": refs[:5],
                    "top1_sim": round(top1, 4),
                }
        else:
            n_unans += 1
            abstained += 1 if not grounded else 0
            risk_cov.append((top1, False))

    overall = metrics.aggregate_retrieval_extended(ans_pairs, _METRIC_K)
    per_type = {
        t: metrics.aggregate_retrieval_extended(p, _METRIC_K) for t, p in sorted(by_type.items())
    }
    snapshot = {
        "note": "PRODUCTION reference — text-embedding-3-small (1536-d); not a CI gate.",
        "k": _METRIC_K,
        "n_answerable": n_ans,
        "n_unanswerable": n_unans,
        "overall": overall,
        "by_type": per_type,
        "answerable_coverage": round(coverage / n_ans, 6) if n_ans else 0.0,
        "unanswerable_abstention": round(abstained / n_unans, 6) if n_unans else 0.0,
        "aurc": metrics.aurc(risk_cov),
        "trip_to_ny": ny,
    }

    k = _METRIC_K
    o = overall
    print("\n" + "=" * 72)
    print(f"RAG CORPUS BASELINE — PRODUCTION embedder (text-embedding-3-small), k={k}")
    print("=" * 72)
    print(
        f"OVERALL  recall@{k}={o['recall_at_k']:.3f}  hit@{k}={o['hit_at_k']:.3f}  "
        f"MRR={o['mrr']:.3f}  nDCG@{k}={o['ndcg_at_k']:.3f}  MAP={o['map']:.3f}"
    )
    for t, m in per_type.items():
        print(
            f"  {t:15} n={m['n']:3}  recall@{k}={m['recall_at_k']:.3f}  hit@{k}={m['hit_at_k']:.3f}"
        )
    print(
        f"ABSTENTION  coverage={snapshot['answerable_coverage']:.3f}  "
        f"abstention={snapshot['unanswerable_abstention']:.3f}  AURC={snapshot['aurc']:.4f}"
    )
    print(f"TRIP-TO-NY ranks: {ny.get('ranks')}")
    print("=" * 72)

    out = metrics.REPORT_PATH.parent / "rag_corpus_production_baseline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    import json  # noqa: PLC0415

    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote production reference: {out}")

    # Loosest sanity only (NOT a quality gate): real semantic embeddings should beat the
    # local feature-hash baseline's overall recall (0.727) on at least the hit metric.
    assert o["n"] == n_ans > 0
    assert (
        o["hit_at_k"] >= 0.5
    ), f"production hit@{k}={o['hit_at_k']:.3f} implausibly low — check the run"
