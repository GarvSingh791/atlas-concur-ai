"""Manual (non-CI) RAGAS-style FAITHFULNESS probe over the full R5 PolicyAgent pipeline (R5-T4).

``@pytest.mark.live`` + ``@pytest.mark.db`` -> SKIPPED by default; runs only under
``make eval-live-r5`` (``LLM_PROVIDER=openrouter`` + ``LLM_API_KEY`` + ``RERANK_PROVIDER=novita`` +
``EMBEDDINGS_PROVIDER=local``) against a live DB.  Where ``make eval`` reports a DETERMINISTIC
faithfulness proxy (token support), this reports the FIRST REAL faithfulness number of the R5
generation stack, judged by Gemini, over ALL answerable gold queries:

  - For each query it runs BOTH (Option B — keeps the retrieval-side metrics as-is while exercising
    the full agent):
      (i)  the real ``PolicyRetrievalTool`` directly — for CONTEXT-PRECISION (rank-aware MAP), the
           enclosing PARENT context (R0-T3 resolver) handed to the judge, and the LOCAL-embedder
           ``max_dense_similarity`` used for the HyDE-trigger probe; and
      (ii) the full ``PolicyAgent.handle()`` path — HyDE-gated retrieval → per-sentence citation
           enforcement (R5-T2) → deterministic NLI/entailment gate (R5-T3) → single audit write —
           producing the answer the SYSTEM actually presents.
  - It then asks a Gemini judge whether every claim in that answer is supported by the retrieved
    context (FAITHFULNESS), and reports citation_completeness + nli_entailment_rate (deterministic,
    from the agent's own answer_structured) alongside.

Novita rerank is throttled to <=30 batches/min (eval-harness-side only; the adapter is never
touched).  Each query performs TWO rerank batches — (i) and (ii) above — so the throttle spaces
every batch >=2.1s (60/30 = 2.0s + margin); effective rate stays <=30/min (wall ~10-12 min).
The 2-batches/query count assumes HYDE_ENABLED=False (the committed default): enabling HyDE adds a
third weak-band rerank batch inside (ii), which the throttle counter would then need to account for.

Self-skips cleanly with no key or no Postgres.  R6-T4 wires the committed faithfulness FLOOR
(thresholds.RAG_FAITHFULNESS_FLOOR) here — enforced ONLY on this keyed run (an LLM judge is
required, so faithfulness is NEVER a CI-on-local gate); the G5-live measurement is DEFERRED.
The same run also enforces thresholds.RAG_GROUNDED_COVERAGE_FLOOR over all answerable cases.
Imports do NO network at module load; the providers are called only inside the test, after skips.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.rag.grounding import CitedAnswer, CitedSentence, check_entailment
from ai_agents.rag.parent_context import resolve_parent_context_for_embedding
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from config.settings import get_settings
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.identity import Organization
from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds._local_embedder import embed
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus.chunking import chunk_document
from evals import thresholds
from evals.harness import metrics
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.local import LocalEmbeddingsProvider
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmMessage, LlmProvider

pytestmark = pytest.mark.live

_K = 5  # context window handed to the generator / judge
_MIN_RERANK_GAP_S = 2.1  # <=30 Novita rerank batches/min (60/30 = 2.0s + margin)


class _RerankThrottle:
    """Spaces Novita rerank batches to <=30/min.  Eval-harness-side ONLY — the Novita adapter is
    never modified (that would throttle every caller, incl. production)."""

    def __init__(self, min_gap_s: float = _MIN_RERANK_GAP_S) -> None:
        self._min_gap = min_gap_s
        self._last = 0.0
        self.batches = 0
        self.total_sleep = 0.0

    def wait(self) -> None:
        sleep_s = max(0.0, self._min_gap - (time.monotonic() - self._last))
        if sleep_s > 0:
            time.sleep(sleep_s)
            self.total_sleep += sleep_s
        self._last = time.monotonic()
        self.batches += 1


def _require_live_key() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.LLM_API_KEY:
        pytest.skip("LLM_API_KEY unset — live RAGAS eval skipped (set LLM_PROVIDER=openrouter).")
    if settings.LLM_PROVIDER == "local":
        pytest.skip("LLM_PROVIDER=local — live RAGAS eval needs a real provider (openrouter).")


def _seed_corpus(session: Session, org: Organization) -> None:
    """Seed the corpus + embeddings under *org* (mirrors seed_all._embed_corpus)."""
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
        for ch in chunk_document(doc):
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=ch.chunk_text,
                    clause_ref=ch.clause_ref,
                    section=ch.section,
                    parent_section_ref=ch.parent_section_ref,
                    ordinal=ch.ordinal,
                    embedding=embed(ch.chunk_text),
                )
            )
    session.flush()


def _judge_faithful_decomposed(
    provider: LlmProvider, answer: str, context: str
) -> list[dict] | None:
    """Statement-level faithfulness judge (replaces the holistic single-shot judge).

    Decomposes ANSWER into atomic SUBSTANTIVE factual/policy claims and judges each against
    CONTEXT.  ATTRIBUTION-EXCLUSION: citation/sourcing statements (which section/label/document
    backs a fact) are NOT factual claims — labels are structural metadata never present in
    chunk_text (same class as the R5 NLI marker-stripping), so judging them by label-string
    match is a category error.  They are excluded at decomposition, and the judge is told to
    ignore labels when scoring support.  Returns the list of judged claims (each
    {"text","supported","reason"}) or None on parse failure (fail-closed, like the prior judge).
    """
    prompt = (
        "You are checking whether a policy assistant's ANSWER is faithful to the CONTEXT it "
        "was given.\n"
        "Decompose the ANSWER into atomic, standalone FACTUAL CLAIMS about what the policy "
        "says (amounts, limits, rules, procedures, eligibility, approvals).\n"
        "EXCLUDE citation/sourcing statements: do NOT emit a claim about WHICH section, label, "
        "document, or reference backs a fact (e.g. 'this is in AIR-06', 'per the FAQ', "
        "'[T&E Policy AIR-06]'). Citation labels are metadata, not factual claims.\n"
        "For each factual claim, decide whether the CONTEXT directly supports it. IGNORE "
        "citation labels entirely when judging support — judge only whether the CONTEXT's "
        "text supports the factual content of the claim.\n"
        'Reply with strict JSON only: {"claims": [{"text": "<claim>", "supported": true, '
        '"reason": ""}, ...]}  where reason is a short phrase ONLY when supported is false '
        '(else "").\n\n'
        f"CONTEXT:\n{context}\n\nANSWER:\n{answer}"
    )
    text = provider.complete([LlmMessage(role="user", content=prompt)]).text.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```")).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("claims")
    if not isinstance(raw, list):
        return None
    claims: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            return None
        supported = c.get("supported")
        text_val = c.get("text")
        if not isinstance(supported, bool) or not isinstance(text_val, str):
            return None
        reason = c.get("reason")
        claims.append(
            {
                "text": text_val,
                "supported": supported,
                "reason": reason if isinstance(reason, str) else "",
            }
        )
    return claims


def _reconstruct_cited(structured: dict) -> CitedAnswer:
    """Rebuild a CitedAnswer from AgentResult.output['answer_structured'] so the eval scores NLI
    with the SAME ``check_entailment`` the agent's gate used — no logic drift between the two."""
    sentences = tuple(
        CitedSentence(
            text=s["text"],
            citations=tuple(
                Citation(
                    kind=CitationKind(c["kind"]),
                    ref=c["ref"],
                    detail=c.get("detail"),
                    source=c.get("source"),
                )
                for c in s["citations"]
            ),
        )
        for s in structured["sentences"]
    )
    return CitedAnswer(sentences=sentences)


def _retrieved_citations(chunks: Sequence[Any]) -> tuple[Citation, ...]:
    """Retrieved chunks -> the clause citations ``GroundedRetriever`` hands the support gate.

    Mirrors ai_agents.rag.retrieval (ref = clause_ref, detail = the leaf chunk text), so
    ``check_entailment(..., retrieved=...)`` here applies the same mis-citation guard the
    PolicyAgent applies (C7R).
    """
    return tuple(
        Citation(
            kind=CitationKind.clause_ref,
            ref=c.clause_ref or "",
            detail=c.chunk_text,
            source=getattr(c, "policy_name", None) or "Policy Document",
            document_type=getattr(c, "document_type", None),
        )
        for c in chunks
    )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@pytest.mark.db
def test_corpus_ragas_faithfulness_live(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """R5-T4: first real faithfulness measurement on the FULL PolicyAgent pipeline over all
    answerable gold queries, with a Novita rerank throttle (<=30/min) + citation/NLI/HyDE probes."""
    _require_live_key()
    settings = get_settings()
    provider = get_llm_provider()
    _seed_corpus(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
    agent = PolicyAgent(provider=provider)
    throttle = _RerankThrottle()

    cases = [c for c in rag_corpus_cases if c.answerable]  # ALL answerable gold queries (145)
    assert cases, "no answerable gold cases resolved — check the corpus dataset"

    lo, hi = settings.RETRIEVAL_SIMILARITY_THRESHOLD, settings.HYDE_WEAK_BAND_UPPER

    faithful_votes: list[bool] = []
    context_precisions: list[float] = []
    citation_completeness: list[float] = []
    nli_rates: list[float] = []
    hyde_trigger = 0
    grounded = needs_review = silent = 0
    genuine_failures: list[dict] = []
    total_claims = 0
    supported_claims = 0

    t_start = time.monotonic()
    for case in cases:
        # (i) retrieval-side probe — Novita rerank batch #1: MAP + parent-context + LOCAL max_dense.
        throttle.wait()
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=_K), context=ctx, session=db_session
        )
        refs = [c.clause_ref for c in r.chunks if c.clause_ref is not None]
        context_precisions.append(metrics.average_precision(refs, case.expected_unit_ids))
        mds = r.max_dense_similarity or 0.0
        if lo <= mds < hi:
            hyde_trigger += 1
        seen: set[str] = set()
        parts: list[str] = []
        for c in r.chunks:
            parent = resolve_parent_context_for_embedding(db_session, c.embedding_id)
            if parent and parent not in seen:
                seen.add(parent)
                parts.append(parent)
        context = "\n\n---\n\n".join(parts)
        # C7R: the retrieved clause set the agent's support gate sees (GroundedRetriever
        # builds one clause_ref citation per top-K chunk, detail = leaf text) — fed to
        # check_entailment below so the mis-citation guard measures the production gate.
        retrieved = _retrieved_citations(r.chunks)

        # (ii) full R5 PolicyAgent path — Novita rerank batch #2 (inside GroundedRetriever): the
        # answer the SYSTEM actually presents (citation-enforced + entailment-gated).
        throttle.wait()
        result = agent.handle(
            Task(context_type="policy_qa", payload={"question": case.query}),
            RoutingDecision(
                target_agent=AiAgent.policy,
                route=AiRoute.llm,
                task_type="policy_qa",
                confidence=1.0,
                rationale="r5-t4-eval",
            ),
            context=ctx,
            session=db_session,
        )
        out = result.output
        answer_type = out.get("answer_type")
        structured = out.get("answer_structured")
        if structured and structured["sentences"]:
            sents = structured["sentences"]
            citation_completeness.append(sum(1 for s in sents if s["citations"]) / len(sents))
            # C7R: the SAME inputs as the agent's gate (question premises, anchor rule and
            # mis-citation guard need the question and the retrieved clause set).
            er = check_entailment(
                _reconstruct_cited(structured),
                threshold=settings.NLI_ENTAILMENT_THRESHOLD,
                question=case.query,
                retrieved=retrieved,
            )
            nli_rates.append(
                sum(1 for sc in er.scores if sc >= er.threshold) / len(er.scores)
                if er.scores
                else 1.0
            )

        answer = out.get("answer")
        if answer_type == "grounded" and answer:
            grounded += 1
            claims = _judge_faithful_decomposed(provider, answer, context)
            if claims is not None:
                overall = all(c["supported"] for c in claims)  # vacuously True if no claims
                faithful_votes.append(overall)
                total_claims += len(claims)
                supported_claims += sum(1 for c in claims if c["supported"])
                if not overall:
                    genuine_failures.append(
                        {"id": case.id, "unsupported": [c for c in claims if not c["supported"]]}
                    )
        elif answer_type == "needs_review":
            needs_review += 1
        else:
            silent += 1
        print(f"[{case.id}] type={answer_type} mds={mds:.3f}")
    elapsed = time.monotonic() - t_start

    n_judged = len(faithful_votes)
    faithfulness = sum(faithful_votes) / n_judged if n_judged else 0.0
    eff_rate = throttle.batches / (elapsed / 60.0) if elapsed > 0 else 0.0

    cc, nr, mp = _mean(citation_completeness), _mean(nli_rates), _mean(context_precisions)
    disp = f"grounded={grounded} needs_review={needs_review} silent={silent}"
    print("\n" + "=" * 72)
    print("R5-T4 LIVE — FIRST real faithfulness measurement on the full R5 PolicyAgent pipeline")
    print("  bars: coin-flip 0.500; R0 token_support proxy ~1.000 (a local-stub artifact,")
    print("  NOT a prior live number — R4 produced no faithfulness baseline)")
    print(f"  cases (answerable) = {len(cases)}")
    print(f"  faithfulness       = {faithfulness:.3f}  [{n_judged} judged / {grounded} grounded]")
    print(f"  citation_complete  = {cc:.3f}")
    print(f"  nli_entail_rate    = {nr:.3f}")
    print(f"  context_prec/MAP   = {mp:.3f}")
    print(f"  disposition        = {disp}")
    print(
        f"  grounded_coverage  = {grounded / len(cases):.3f}  "
        f"[floor {thresholds.RAG_GROUNDED_COVERAGE_FLOOR}]"
    )
    print(f"  hyde_trigger       = {hyde_trigger}/{len(cases)} (max_dense in [{lo:.2f}, {hi:.2f}))")
    print("     ^ LOCAL embedder max_dense_similarity (EMBEDDINGS_PROVIDER=local) — the scale")
    print(f"       HYDE_WEAK_BAND_UPPER={hi} was fit on; NOT a production-embedder rate.")
    print(f"  novita rerank      = {throttle.batches} batches, {throttle.total_sleep:.1f}s sleep,")
    print(f"                       effective {eff_rate:.1f}/min (<=30), wall {elapsed:.0f}s")
    print("=" * 72)
    claim_frac = supported_claims / total_claims if total_claims else 0.0
    print(f"  claim_frac_support = {claim_frac:.3f}  [{supported_claims}/{total_claims} claims]")
    print(f"  unfaithful_answers = {len(genuine_failures)}")
    for _gf in genuine_failures:
        print(f"    [{_gf['id']}] unsupported claim(s):")
        for _u in _gf["unsupported"]:
            print(f"      - {_u['text']}  (reason: {_u['reason']})")

    # Coverage floor FIRST: a run that withheld most answers (gate likely too strict) must fail on
    # that, not on a confusing judge-parse message or pass on a handful of faithful answers.
    metrics.require_grounded_coverage(grounded, len(cases), thresholds.RAG_GROUNDED_COVERAGE_FLOOR)
    assert n_judged >= max(1, grounded // 2), "judge failed to parse too many replies"
    # R6-T4: the committed faithfulness FLOOR (thresholds.RAG_FAITHFULNESS_FLOOR), enforced ONLY on
    # this keyed eval-live-r5 run — an LLM judge is required, so faithfulness is NEVER a CI-on-local
    # gate.  G5-live measurement is DEFERRED; the floor is a conservative placeholder above the 0.5
    # coin-flip, to be tightened once a keyed baseline exists.
    floor = thresholds.RAG_FAITHFULNESS_FLOOR
    assert faithfulness >= floor, (
        f"live faithfulness {faithfulness:.3f} < committed floor {floor} "
        f"(R6-T4 keyed-run gate; the deterministic CI build never judges faithfulness)."
    )
