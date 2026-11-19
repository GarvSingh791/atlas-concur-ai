# R5-T1 + R5-T2 — HyDE + document-qualified citation enforcement (as-built)

**Date:** 2026-07-01
**Author:** Garv Singh Saini
**Branch:** r5-query-gen-hardening (off main @ R4-closed, tip `41baeee`; uncommitted at authoring)

Adds two generation-quality layers, both **default-OFF / CI byte-identical to R4**. R5-T1
wires a latency-gated HyDE (Hypothetical Document Embeddings) second retrieval pass into
`GroundedRetriever` for lexically-weak queries. R5-T2 adds per-sentence, document-qualified
citation enforcement — the answer is decomposed into a machine-readable `CitedAnswer` whose
citations come STRICTLY from the LLM's verified inline `[T&E Policy §AIR-01]` markers.

## Scope
R5-T1 (conditional HyDE, latency-gated) + R5-T2 (per-sentence document-qualified citation
enforcement + structured `CitedAnswer`). OUT: R5-T3 (NLI grounding check), R5-T4 (eval), the
abstention gate predicate / τ* / calibration.py, fusion.py / RRF-k / reranker, any pgvector
schema/migration, frontend (R6-T2), the `ai_decision_logs` audit-write path.

## Decisions (FORK-M…P + two adversarial-finding fixes)
- **FORK-M (HyDE placement).** Two-pass flow lives INSIDE `GroundedRetriever`: `retrieve()`
  is a thin orchestrator that runs pass-1, calls `_maybe_hyde()` only when `HYDE_ENABLED`,
  then grounds the chosen result via a new `_ground()` method that holds the pre-R5 gate/
  build body VERBATIM. `_maybe_hyde` fires the second pass on a fresh `RetrievalIntent(query=
  hypothesis)` (no embedder needed — the tool embeds any query string) and keeps whichever
  pass has the stronger `max_dense_similarity`. Under `HYDE_ENABLED=False` the whole block is
  structurally skipped → `retrieve()` is byte-identical to the pre-R5 single-pass flow.
  τ*/`top_similarity` are (re)computed INSIDE `_ground` so the module-level monkeypatch seams
  (`load_abstention_threshold`, `resolve_parent_context_for_embedding`) the tests rely on keep
  applying to the gate.
- **FORK-N (weak-band signal) — APPROVED FORK (pre-coding sign-off).** Signal = the top
  `max_dense_similarity`, NOT the dual-scale gate-aligned score the task spec recommended. The
  gate is dual-scale (cosine-0.20 floor locally; calibrated-Platt-τ* floor in prod) so a
  single `HYDE_WEAK_BAND_UPPER` would be incoherent across both; `max_dense_similarity` is one
  stable cosine `[0,1]` signal, always present, and the DIRECT measure of the dense-match
  weakness HyDE fixes. Exact conditional: `md is not None and self._threshold <= md <
  self._hyde_weak_band_upper` → `0.20 <= md < 0.30`. Default `HYDE_WEAK_BAND_UPPER=0.30`
  (~14% into the LOCAL on-policy band [0.25,0.60]; the docstring flags it as a per-embedder
  number, inert while the flag is off).
- **FORK-O (CitedAnswer + strict enforcement) — YOUR STRICT CHOICE.** New pure module
  `ai_agents/rag/grounding.py`: `CitedSentence`/`CitedAnswer` frozen dataclasses (`full_text`,
  `__str__ → full_text`, `needs_review`, `distinct_citations`, `to_dict`), `citation_label`,
  `extract_cited_sentences`. Integration: `PolicyQaHypothesizer.seed()` post-processes the
  LLM answer → `GroundedResult(value=cited, citations=cited.distinct_citations())` with **NO
  fallback** — a fully-uncited answer yields zero citations and does not assert. Empty/
  ProviderError → `value=""` (FALSY) so `PolicyQaCritic`'s empty short-circuit still fires.
  Critic contract intact via `CitedAnswer.__str__`.
  - **DEVIATION (code wins):** `extract_cited_sentences(raw_text, available_citations)` REUSES
    the retrieval `Citation` objects (matched by `ref`) rather than rebuilding them from
    clauses — this preserves the leaf-unit `detail` + `policy_name` source and avoids expanding
    `GroundedClause` beyond `document_type`. `citation_label` falls back to `"Policy"` when
    `document_type` is NULL (the seeded corpus always carries one; only the legacy stub is
    NULL).
- **FORK-P (document_type).** `document_type: str|None=None` added additively to
  `RetrievalCandidate`, `RetrievedChunk`, `GroundedClause`; `p.document_type::text` added to
  BOTH the dense and sparse SELECTs (JOIN to `policies` already present) and BOTH candidate
  constructors; propagated → `RetrievedChunk` → `GroundedClause` → `GroundedRetrieval.to_dict()`.
  Typed `str|None` (never the `PolicyDocumentType` enum — layering). No migration: the column
  is from migration 0008 (`policy_document_type` PG enum, values policy|faq|howto|reference).
  The `::text` cast is SELECT-list only — row order/gate untouched.

### Adversarial-finding fixes (both real, both fixed + tested)
- **Per-sentence strict gate (material behavior change).** A partially-cited answer (one cited
  sentence + one uncited/hallucinated sentence) was asserting `grounded` with wire
  `needs_review=False` because `validate_grounding` only checks `len(citations) >= 1`.
  `policy_agent._handle_qa` now computes `fully_grounded = result.asserted AND NOT
  cited.needs_review`; a partially-cited answer becomes **`needs_review`, `answer=None`**, with
  the per-sentence breakdown retained in `answer_structured` (`CitedAnswer.to_dict()`) for
  human review / R6-T2 chips. This is the correct reading of the R5-T2 DoD ("every answer
  sentence must cite"); NLI validity of each cited claim is R5-T3.
- **NUL-byte robustness.** `extract_cited_sentences` raised `IndexError` on a `\x00N\x00`-shaped
  input (the masking sentinel collided); NUL bytes are now stripped up front, upholding
  reflect()'s "never raises" contract. Real providers never emit NUL — latent defect closed.

## CI-safety (the R5-T1 hinge)
Under the committed default `HYDE_ENABLED=False`, `_maybe_hyde` is never entered — zero LLM
calls. A `_RaisingProvider` spy (whose `complete()` raises) installed through the FULL
`retrieve()` path never fires, both under `HYDE_ENABLED=False` AND under `HYDE_ENABLED=True` +
`LLM_PROVIDER=local` (the deterministic rewriter returns the query unchanged → `hypothesis ==
query` → second pass skipped). `generate_hyde_doc` is fail-safe: ProviderError / empty
completion → the original query. eval-db byte-identity independently confirms no LLM leaked
into the CI path.

## Verification (Section 10)
- **G1** ruff check + format --check clean (all touched files).
- **G2** mypy clean — CI target `backend config evals` (146 files) + the new `rag/query_rewrite.py`
  + `rag/grounding.py` + touched modules.
- **G3** 445 passed / 161 deselected (full hermetic `ai_agents` suite); backend policy +
  conversations API 32 passed. Sub-cases: R5-T1 (a) spy zero-LLM under both disabled AND
  local-enabled paths; (b) weak-band fires once / skips strong / skips below-floor / keeps
  better pass; (c) ProviderError + empty → query unchanged. R5-T2 (a-e) valid / uncited-flagged
  / hallucinated-rejected / multi-cite / `§LOD-01.` & `§4.2.1` boundary + NUL-robustness;
  (f) `citation_label` all 4 doctypes + None + no-ref; (g) critic tests green UNCHANGED;
  (h) GroundedRetriever tests green UNCHANGED; per-sentence strict gate + JSON-safety
  (`json.dumps(dict(output))`) covered. Layering: both new modules in
  `test_no_backend_import._IMPORT_SCRIPT`, subprocess test green.
- **G4** eval-db (local embedder, PYTHONHASHSEED=0, zero external calls): **47 passed —
  recall@10 = 0.819031, coverage = 1.0, abstention = 0.019231 — BYTE-IDENTICAL to R4**,
  re-run twice (before + after the finding fixes). The only retrieval-path change
  (`document_type::text` in the SELECT) is additive and does not alter ordering/gate.
- **G7** trip-to-NY (ans-142): `answerable_coverage = 1.0` proves it still grounds under the
  committed default; `citation_label` document-qualification unit-tested for all 4 doctypes.
- **G8** zero added query-time latency under the committed default (the HyDE branch is not
  entered; `policy_retrieval.py` embed/rerank path untouched).

## Files (13 code + this log)
`ai_agents/rag/query_rewrite.py` (new) · `ai_agents/rag/grounding.py` (new) ·
`ai_agents/tests/test_query_rewrite.py` (new) · `ai_agents/tests/test_grounding.py` (new) ·
`ai_agents/tests/test_hyde_retrieval.py` (new) ·
`ai_agents/tests/test_r5_citation_enforcement.py` (new) · `config/settings/base.py` ·
`ai_agents/rag/retrieval.py` · `ai_agents/tools/policy_retrieval.py` ·
`ai_agents/agents/policy_qa.py` · `ai_agents/agents/policy_agent.py` ·
`ai_agents/tests/test_no_backend_import.py` · `ai_agents/tests/test_policy_agent.py`.

## Carried-forward / deferred
- **R5-T3 (NLI grounding check):** deterministic per-claim entailment supplement to the LLM
  critic; verifies each CITED sentence actually follows from its citation (R5-T2 enforces
  citation PRESENCE; R5-T3 enforces VALIDITY). Reuses the single audit write (Fence 11).
- **R5-T4 (eval):** RAGAS faithfulness / context-precision delta vs R4; HyDE trigger-rate +
  latency evidence.
- **`HYDE_ENABLED=True` production validation:** eval-live with Gemini (OpenRouter) + Novita
  reranker — **throttle to 30 req/min** (Novita account cap). Validate HyDE fires on the
  correct weak-band queries and improves results before flipping the production default.
- **Frontend citation chips (R6-T2):** consume `answer_structured` (`CitedAnswer.to_dict()`).

## Process note
A 7-dimension adversarial verification cleared 5 dimensions (byte-identical,
ci-safety, json-safety, fork-p, layering) and confirmed 2 issues (per-sentence strict gate,
NUL robustness) — both fixed above. 2/5 verify passes did not complete (a
tooling hiccup, not a code defect); the 5 clean dimensions were independently validated by
direct gate runs (eval-db byte-identical, layering subprocess test, JSON-safety unit test,
`_RaisingProvider` spy), and the two finding-producing dimensions were hand-reviewed. G1/G2/G3/G4
were re-run directly rather than trusting any workflow self-report.
