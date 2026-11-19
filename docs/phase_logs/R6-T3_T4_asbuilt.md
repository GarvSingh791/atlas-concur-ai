# R6-T3 + R6-T4 — Audit Completeness + Full Regression Eval in CI (as-built)

**Date:** 2026-07-02
**Author:** Garv Singh Saini
**Branch:** r6-audit-regression (off main@6bfaae6 = R6-T1+T2; parent 7e31aba = R5;
uncommitted at time of writing)

Closes the four numbered R6 production-hardening tasks (T1–T4). R6-T3 makes the single
decision-log write tell the full retrieval story; R6-T4 completes the durable CI regression
gate and its build artifacts. Phase R6's public write-up deliverable and the keyed live
runs (G5-live faithfulness, p50/p95) remain.

## Scope
R6-T3 (enrich the existing grounded + abstain decision-log writes with the full retrieval
story — candidate set [document_id + unit ID], fused ranks, reranker scores, gate signal +
source, decision, document-qualified citations — via additive nullable propagation fields;
NO schema migration, NO second write) + R6-T4 (committed regression floors that fail CI on a
recall/precision/nDCG/AURC drop; overall + per-doc-type metrics table + risk-coverage CSV as
CI artifacts; faithfulness floor wired into the keyed live harness only). OUT: the public
writeup; live faithfulness/p50/p95 (keyed, deferred); correlation_id/latency_ms (already
present); GET /audit endpoint (audit-serving, not audit-completeness).

## A0 + B0 reconciliation (code wins — five stale spec/memory premises corrected)
| Spec/memory said | Tree reality | Verdict |
|---|---|---|
| R6-T1+T2 uncommitted on main | committed as 6bfaae6; tree clean | CODE-WINS (STOP-gate not triggered) |
| latency_ms unpopulated on policy path | already `int((time.monotonic()-t0)*1000)` on both writes | CODE-WINS (only asserted) |
| NO correlation_id column | exists (ai_decision_log.py:86, migration 0007, indexed) + written | CODE-WINS (untouched; no migration) |
| figures.py = floors source | that's domain figures; floors live in evals/thresholds.py | CODE-WINS (extend thresholds.py) |
| R6-T4 builds full-gold-set eval anew | eval-db already runs the 197-query set + emits/uploads metrics.json | CODE-WINS (extends eval-db) |

## R6-T3 — audit completeness

### Propagation seam (A0 finding)
Per-candidate document_id (policy_id) + RRF rank lived on the intermediate RetrievalCandidate
but died at the RetrievedChunk build; rerank_score / calibrated_score / confidence_source /
tau_star never reached the write site. The grounded write recorded only {question_len} /
{clause_count} / {answer_type, asserted, nli_result} — nothing about how the answer was
retrieved.

### Additive fix (analogous to R1's max_dense_similarity)
New frozen `CandidateAudit(document_id, unit_id, fused_rank, rerank_score)` in
tools/policy_retrieval.py; additive nullable `RetrievalResult.audit_trail` built where
fused/reranked/rerank_scores coexist (fused_rank via an {embedding_id: rank} map over fused);
additive nullable `GroundedRetrieval.retrieval_audit` set on all three _ground returns
(grounded, below-threshold silent, no-chunks silent), NOT in to_dict(). document_id now
propagates per-candidate (it did not before). confidence_source / tau_star / threshold read
directly off GroundedRetrieval (R6-T2). Pure observability — no retrieval/gate behavior
change -> eval-db byte-identical.

### Enriched writes — BOTH branches, fence preserved
A shared `_audit_detail(retrieval, *, decision)` enriches the existing DecisionRecord on
BOTH mutually-exclusive QA branches (each session.flush() only, no commit): the grounded
write (decision="answer") and the abstain/silent write (decision="abstain" — records the
below-threshold candidate set + gate signal that EXPLAINS the abstention; citations=[]; the
no-chunks case records candidate_count=0). Fence held: exactly 1 write per invocation,
mutually exclusive, len(records)==1, re-asserted (the R5 mutation-tested invariant).

### Enriched JSONB shape (identity + numerics; no raw-text dumps)
The grounded write's three JSONB columns, after minimize():

    input_summary : { question_len, query }
    tools_used    : { GroundedRetriever: { clause_count, candidate_count,
                        candidates:[ {document_id, unit_id, fused_rank, rerank_score}, ... top_n ],
                        gate:{ source, signal, threshold, tau_star } },
                      LlmProvider:{ model } }
    output_summary: { answer_type, asserted, decision, nli_result,
                      citations:[ {kind, ref, detail, source, document_type} ] }

- `candidates` = the reranked top-N (the returned chunk count — cap by construction, NOT the
  ~50+50 pre-fusion set); identity + numerics ONLY, never chunk_text.
- `gate.source` in {"cosine","calibrated"} mirrors R6-T2's confidence_source: "cosine" on the
  local/CI path (tau_star null), "calibrated" only on the keyed Novita path (tau_star =
  0.039299). `signal` is the gate-basis score; None on the silent path.
- `citations` carry a snippet (`detail`) for the CITED clauses ONLY — the stated honest
  exception to "no raw text"; bounded to the answer's own evidence.
- The abstain row is identical minus `citations` (empty) with `decision="abstain"`; the
  candidate set + gate explain why it abstained.

### Sample grounded row (local hermetic path)
    { "input_summary": {"question_len":45,"query":"Can I fly business class on a 12-hour flight?"},
      "tools_used": {"GroundedRetriever":{"clause_count":1,"candidate_count":2,
        "candidates":[{"document_id":"550e8400-...","unit_id":"AIR-CABIN","fused_rank":1,"rerank_score":0.8123},
                      {"document_id":"6ba7b810-...","unit_id":"LOD-01","fused_rank":3,"rerank_score":0.4106}],
        "gate":{"source":"cosine","signal":0.92,"threshold":0.2,"tau_star":null}},"LlmProvider":{"model":"..."}},
      "output_summary": {"answer_type":"grounded","asserted":true,"decision":"answer","nli_result":{...},
        "citations":[{"kind":"clause_ref","ref":"AIR-CABIN","detail":"Business class is only allowed ...",
                      "source":"Helios T&E Policy","document_type":"policy"}]},
      "confidence":0.92,"reflection_outcome":"passed","latency_ms":0 }   // GROUNDED_WRITE_COUNT: 1

`latency_ms=0` is the hermetic stub path (sub-millisecond rounds to 0); the completeness test
asserts non-null, which 0 satisfies — real latency populates on the keyed path. The row makes
the source document explicit (550e8400... / AIR-CABIN / document_type=policy).

### minimize() safety (NFR-SEC5)
The existing minimize() redactor (decision_log/minimizer.py) runs over all three JSONB columns
before persistence. It redacts sensitive KEY names (password|secret|token|api_key|
authorization|...) and masks value patterns (bearer / email / 16-digit card), and passes
non-string primitives (int, float, UUID-as-string) through unchanged. Verified: UUIDs, clause
refs, fused_rank ints, and rerank_score floats survive; an email/bearer/card/api_key payload
is redacted. No legitimate audit ID or score is stripped; no secret leaks.

### Tests (hermetic, +11, additive)
New ai_agents/tests/test_r6_audit_completeness.py mirrors the R5 fence-test harness (scripted
provider + patched GroundedRetriever.retrieve + captured DecisionRecord). Covers: single
grounded write (decision="answer"), single abstain write (decision="abstain"), mutual
exclusion (disjoint decisions, len==1 each), full-stage completeness (every stage present in
one row + which document + non-null latency_ms), and minimize preservation/redaction. eval-db
stays byte-identical (47 passed after Part A — the completeness tests are Tier-1 hermetic, not
in the db tier).

## R6-T4 — full regression eval in CI

### Extends eval-db (no new job)
The full 197-query gold set already runs in `make eval-db` (pytest backend/tests/eval -m db,
LLM_PROVIDER=local, PYTHONHASHSEED=0). R6-T4 extends that path — it adds floors + assertions +
a per-doc-type breakout + CSV artifacts; it does NOT add a parallel job. This auto-satisfies
"full-gold-set recall consistent with eval-db" (same run).

### Committed floors (evals/thresholds.py — extends the RAG_CORPUS_* block)
Anchored below/above the measured LOCAL-PATH run, enforced by test_r6t4_regression_floors:
| metric | floor/ceiling | measured (local) |
|---|---|---|
| recall@10 | >= 0.80 | 0.819031 |
| nDCG@5 | >= 0.63 | 0.658420 |
| precision-on-answered | >= 0.20 (collapse-guard) | 0.244138 |
| AURC | <= 0.09 | 0.084096 |
| coverage / abstention | == 1.000000 / == 0.019231 | exact |
| per-doc-type unit-recall | faq .95 / howto .95 / policy .55 / reference .65 | held |

precision-on-answered @5 = 0.244138 is expected-low on a small gold set (few relevant clauses
per query); the 0.20 floor catches a precision COLLAPSE toward zero — it is NOT a precision
quality claim. Per-doc-type floors are on RECALL, not precision, because per-type precision
cells are small-n artifacts (see below).

### Per-doc-type table + answered-count n
_score_corpus now also groups answerable queries by doc type (single-type queries -> their
type; multi-type -> cross_document) and emits per-type recall@10 / nDCG@5 /
precision-on-answered / coverage + the answered-count n. The n column exists BECAUSE some
cells are small-n (faq n=1, reference n=4) — a 1.000 or 0.200 precision cell at n<=4 is a
low-support artifact, not signal:

    doc_type,      n_answered, recall_at_10, ndcg_at_5, precision_on_answered, coverage, abstention, aurc
    overall,       145,        0.819031,     0.65842,   0.244138,              1.0,      0.019231,   0.084096
    cross_document,92,         0.736517,     0.613407,  0.278261,              1.0,      N/A,        N/A
    faq,           1,          1.0,          1.0,       0.2,                   1.0,      N/A,        N/A
    howto,         33,         1.0,          0.836638,  0.2,                   1.0,      N/A,        N/A
    policy,        15,         0.866667,     0.569433,  0.16,                  1.0,      N/A,        N/A
    reference,     4,          1.0,          0.471713,  0.15,                  1.0,      N/A,        N/A

abstention + AURC are corpus-level (the unanswerable set is out-of-corpus / untyped) -> N/A
per doc type.

### Risk-coverage curve (tau-sweep) + artifacts
New metrics.risk_coverage_curve_with_tau emits (tau, coverage, risk) rows (the existing
risk_coverage_curve / aurc are untouched -> AURC byte-identical). test_emit_corpus_metrics_report
writes two flat CSVs alongside metrics.json:
- evals/reports/rag_corpus_metrics_table.csv (the overall+per-doc-type table above)
- evals/reports/risk_coverage.csv (197 tau-sweep rows)
Both are gitignored CI artifacts (regenerate every run), uploaded by .github/workflows/ci.yml
(path: evals/reports/). NO PNG — no plotting dependency exists and none was added
(honest-skip); the curve DATA is the machine-readable artifact. Only evals/reports/README.md
(the honest split) is tracked, via the .gitignore idiom `evals/reports/*` +
`!evals/reports/README.md`.

### Faithfulness — keyed only (DEFERRED)
RAG_FAITHFULNESS_FLOOR=0.80 is asserted ONLY in the keyed evals/manual/test_rag_corpus_ragas_live.py
(make eval-live-r5, LLM_PROVIDER=openrouter + keys), replacing the old loose >= 0.5 coin-flip.
It self-skips without keys. The deterministic CI build (LLM_PROVIDER=local) has NO LLM judge,
so it structurally CANNOT and DOES NOT gate faithfulness — the test is path-excluded from the
eval-db job (it lives in evals/manual/, not backend/tests/eval/) and self-skips in the
hermetic backend job. G5-live measurement is DEFERRED. CI never claims to gate faithfulness
(stated in evals/reports/README.md + thresholds.py).

### Floor-trip proof
Tripping RAG_CORPUS_NDCG_AT_5_FLOOR 0.63 -> 0.99 (above measured) fails eval-db with a clear
message:

    E   AssertionError: nDCG@5=0.658420 < floor 0.99 (R6-T4 regression guard).
    FAILED backend/tests/eval/test_rag_corpus_eval.py::test_r6t4_regression_floors -- 1 failed

Reverted -> 0.63; 1 passed. thresholds.py sits at measured-baseline values (no tripped value
left in the diff).

## Gates (from the FSR; NOT re-run in this step)
- Part A: hermetic ai_agents suite green (+11 new); eval-db byte-identical (47 passed);
  single-write fence (1 answer + 1 abstain, mutually exclusive); ruff/mypy clean; git diff
  database/ config/ EMPTY.
- Part B: eval-db 48 passed byte-identical (recall@10=0.819031, coverage=1.000000,
  abstention=0.019231 reproduced exactly); mypy backend config evals clean (148); ruff clean;
  no ai_agents/database/config change; no plotting dependency added.
- CI metrics are LOCAL-PATH plumbing numbers (blake2b + cosine gate + RRF-anchored local
  reranker) — byte-reproducible; they lock the plumbing, NOT the production calibrated numbers
  (tau*=0.039299). recall@10=0.819031 is the local number, not the production figure.

## Adversarial verification (two 4-lens review rounds)
- Part A (R6-T3): 4/4 lenses PASS — single-write fence (both branches), additive/no-behavior-
  change, honesty anchors (identity+numerics, minimize safety), layering/diff-discipline. All
  findings nit severity.
- Part B (R6-T4): 3/4 PASS — honesty-split, byte-identical/additive, diff-discipline. The
  floors-soundness verifier lens did not complete (tooling error) and was NOT re-run; its
  coverage is the floor-trip demo (nDCG floor proven to fail+revert) plus the by-inspection
  check that every floor sits on the correct side of its measured value.
- Declined nits (with rationale): (1) audit-trail build not try/except-wrapped — reads
  required frozen fields, cannot raise; matches R1's unwrapped max_dense. (2) query/citation
  payload growth — this is the required enrichment; minimize() covers PII; citations bounded
  to cited clauses. Two "ai_agents out of Part B scope" nits are false-positives of the
  isolated Part-B verifier framing (the ai_agents changes are R6-T3, in-scope, metric-inert).

## Files (3 new + 9 modified)
New:
- ai_agents/tests/test_r6_audit_completeness.py (R6-T3 hermetic completeness + fence + minimize)
- evals/reports/README.md (the honest CI-vs-keyed split; tracked via .gitignore negation)
- docs/phase_logs/R6-T3_T4_asbuilt.md (this file)
Modified (312 insertions / 19 deletions over the 9 non-phase-log files):
- ai_agents/agents/policy_agent.py (R6-T3: _audit_detail + both enriched writes)
- ai_agents/rag/retrieval.py (R6-T3: GroundedRetrieval.retrieval_audit on 3 returns)
- ai_agents/tools/policy_retrieval.py (R6-T3: CandidateAudit + RetrievalResult.audit_trail)
- backend/tests/eval/test_rag_corpus_eval.py (R6-T4: per-doctype metrics, floor test, CSV emit)
- evals/harness/metrics.py (R6-T4: risk_coverage_curve_with_tau)
- evals/manual/test_rag_corpus_ragas_live.py (R6-T4: faithfulness floor, keyed)
- evals/thresholds.py (R6-T4: committed floors/ceilings)
- .github/workflows/ci.yml (R6-T4: upload evals/reports/ dir)
- .gitignore (R6-T4: track evals/reports/README.md, ignore generated artifacts)
git diff database/ = EMPTY (no schema migration); git diff config/ = EMPTY.

## Deferred / open risks
- G5-live faithfulness (RAGAS via Gemini) + live p50/p95 — keyed workflow_dispatch, self-skip
  proven; DEFERRED.
- The 0.70 NLI near-extractive threshold (R5 carry-forward) still stands — token-overlap has no
  synonym/negation awareness; fail-safe (-> needs_review) + config-tunable; watch
  nli_entailment_rate on the keyed run.
- Phase R6 public write-up deliverable — not this task.

## Honesty anchors (binding)
Audit rows are structured identity+numerics, never raw-text dumps. No schema migration
(correlation_id + latency_ms already exist and are populated). The single-audit-write fence
held on both branches. CI gates the deterministic local-path plumbing metrics, NOT faithfulness
and NOT the production calibrated numbers. precision floor 0.20 is a collapse-guard, not a
quality claim. per-type precision cells are small-n artifacts (why n_answered is emitted + why
per-type floors are on recall). CSVs are gitignored regenerated artifacts; no PNG (no plotting
dep). tau*=0.039299 / HYDE_ENABLED=False / NLI 0.70 UNCHANGED.
