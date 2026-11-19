# R5-T3 + R5-T4 — NLI Grounding Check + Faithfulness Eval Harness (as-built)

**Date:** 2026-07-01
**Author:** Garv Singh Saini
**Branch:** r5-query-gen-hardening (off main@979401b = R5-T1+T2 committed; uncommitted here)

Adds the deterministic NLI entailment grounding check (R5-T3) and rewires the
RAGAS faithfulness eval to run through the full PolicyAgent path over all 145 answerable
queries with a Novita throttle (R5-T4). Phase R5 is CODE-COMPLETE; G5-live is deferred
to a keyed env — one make eval-live-r5 run closes R5 fully.

## Scope
R5-T3 (EntailmentResult + check_entailment in grounding.py; NLI_ENTAILMENT_THRESHOLD in
config; NLI gate injected into policy_agent.py before the single audit write) + R5-T4
(test_rag_corpus_ragas_live.py rewired through PolicyAgent.handle() over all 145 answerable
queries with 2.1s Novita throttle + citation/NLI/HyDE metrics; eval-live-r5 Makefile
target). OUT: τ*/calibration; fusion/RRF; pgvector schema; frontend (R6); Novita adapter
internals; HYDE_ENABLED default (stays False); production LLM-backed entailment (optional
future enhancement).

## R5-T3 — NLI grounding check

### Architecture

```
ai_agents/rag/grounding.py additions (pure, stdlib only):
  _ENTAIL_WORD_RE = re.compile(r"[a-z0-9]+")
  _DEFAULT_ENTAILMENT_THRESHOLD = 0.70
  _token_support(text, context) -> float   # inlined verbatim from metrics.token_support
  EntailmentResult(entailed: bool, min_score: float,
                   scores: tuple[float,...], threshold: float) [frozen, to_dict()]
  check_entailment(cited: CitedAnswer, *, threshold: float) -> EntailmentResult
```

### Key implementation finding (code wins) — marker stripping
`check_entailment` strips `[Label §ref]` citation markers from each sentence before
computing token overlap. Without stripping, even a verbatim answer scores 0.667 because
the marker tokens (e.g. "T&E", "Policy", "AIR-01") are metadata that don't appear in
the cited chunk text. Verified by a 200k-case fuzz proving the inlined `_token_support`
is byte-identical to `metrics.token_support`.

### NLI gate integration (FORK-R — single write, Fence 6/Rule 11)
Injected in `policy_agent.py::_handle_qa` after the R5-T2 strict gate (:149):
```python
nli = check_entailment(cited, threshold=settings.NLI_ENTAILMENT_THRESHOLD)
    if cited is not None else None
fully_grounded = strict_grounded and (nli is None or nli.entailed)
```
`nli_result: nli.to_dict()` added to the existing `output_summary` dict before the
**single** `write_decision_log` call at :184. Zero second write. Mutation-tested:
changing `and` to `or` breaks exactly sub-case (d). grep `write_decision_log`
`policy_agent.py` → 6 lines (1 import at :23 + 5 call-sites :99/:184/:242/:275/:313),
exactly 1 call on the grounded Q&A path (:184); :99 silent, :242/:275/:313 Cap-3 authoring.

### FORK-Q — inline token_support (layering)
`grounding.py` imports only `guardrails.citations` + stdlib. Importing from `evals.*`
would add an `ai_agents→evals` edge the module doesn't have. The ~8-line function is
inlined; byte-identity with `metrics.token_support` proven by the 200k fuzz.

### Known threshold risk (⚠ LEADS the carried-forward)
`NLI_ENTAILMENT_THRESHOLD=0.70` is near-extractive; set-based token overlap has no
synonym/negation awareness. Faithful paraphrases can be downgraded:
- Synonym-heavy answer: score 0.500 (< 0.70) → needs_review
- Correct negation: score 0.364 (< 0.70) → needs_review
This is fail-safe (only → needs_review, never asserts unsupported) and one-line tunable.
**First action after eval-live-r5 runs**: read `nli_entailment_rate` and `disposition`
(grounded/needs_review/silent counts). If grounded answers are broadly withheld, lower
to ~0.50–0.60 and re-run. The eval's `grounded==0` guard surfaces this explicitly.

### Fixture update
`test_r5_citation_enforcement.py`: 1 fixture changed from an inferential answer
(score 0.667 < 0.70, would now fail NLI) to an extractive answer. The fixture's
JSON-safety purpose is preserved; the answer now correctly passes the NLI gate.

## R5-T4 — Faithfulness eval harness

### FORK-S (Option S-A + Option B retrieval)
`test_rag_corpus_ragas_live.py` rewired: generation routes through
`PolicyAgent.handle()` (exercises the full R5 pipeline: citation enforcement + NLI
gate); retrieval diagnostics keep the direct `tool.retrieve()` call for MAP +
parent-context + `max_dense_similarity` HyDE probe. 2 Novita batches per query.
Zero production-contract change.

### Expansion and throttle (FORK-T)
All 145 answerable gold queries (was a 10-id hardcoded subset). `_RerankThrottle`
(module-level monotonic clock) sleeps `max(0, 2.1 - elapsed)` before each of the
2 Novita-touching calls per query. Sliding-window analysis: ≤29 batches/60s (≤30
HARD). Wall time ~10–12 min. Novita adapter untouched.

### New metrics printed
- `citation_complete`: fraction of answer sentences with ≥1 verified citation
- `nli_entail_rate`: fraction of sentences passing `check_entailment` at threshold
- `hyde_trigger`: N/145 queries with `max_dense_similarity ∈ [0.20, 0.30)` — measured
  on the LOCAL embedder (`EMBEDDINGS_PROVIDER=local`), the scale `HYDE_WEAK_BAND_UPPER
  =0.30` was fit on — NOT a production-embedder trigger rate
- Novita throttle summary: batches, total sleep, effective rate (≤30/min)
- Disposition: grounded=M / needs_review=P / silent=Q counts

### G5-live status — DEFERRED
No `LLM_API_KEY` / `RERANK_API_KEY` in this env → eval self-skipped cleanly
(1 skipped, 0 failures). The harness is code-complete. Under `make eval-live-r5`
in a keyed env it will print verbatim:
```
================================================================
R5-T4 LIVE — FIRST real faithfulness measurement on the full R5 PolicyAgent pipeline
  bars: coin-flip 0.500; R0 token_support proxy ~1.000 (local-stub artifact)
  cases (answerable) = 145
  faithfulness       = X.XXX  [N judged / M grounded]
  citation_complete  = X.XXX
  nli_entail_rate    = X.XXX   <- PRIMARY DIAGNOSTIC (see threshold risk above)
  context_prec/MAP   = X.XXX
  disposition        = grounded=M needs_review=P silent=Q
  hyde_trigger       = K/145 (LOCAL embedder max_dense in [0.20, 0.30))
  novita rerank      = 290 batches, XXX.Xs sleep, effective YY.Y/min (<=30)
```

Honest framing: first real faithfulness measurement of the R5 pipeline. No delta vs R4
is claimed — R4 produced no faithfulness baseline (its live matrix was abandoned).

## Verification
G1 ruff check + format --check → All checks passed! (6 files already formatted).
G2 mypy → Success: no issues found in 146 source files + 8 ai_agents source files.
G3 454 passed / 0 failures / 0 errors (445 R5-T1+T2 baseline + 9 new NLI tests).
G4 make eval-db → 47 passed, recall@10=0.819031, coverage=1.000000, abstention=0.019231
   — BYTE-IDENTICAL to R5-T1+T2 (re-run explicitly; PG reachable this session).
G5-live DEFERRED (eval self-skips cleanly; code-complete).

## Files (7 code + this log)
**New:** ai_agents/tests/test_r5_nli_grounding.py.
**Modified:** ai_agents/rag/grounding.py · config/settings/base.py ·
ai_agents/agents/policy_agent.py · ai_agents/tests/test_r5_citation_enforcement.py ·
evals/manual/test_rag_corpus_ragas_live.py · Makefile.

## Phase R5 DoD checklist
| Item | Status | Evidence |
|---|---|---|
| Conditional HyDE on weak retrieval only | ✓ | HYDE_ENABLED=False; probe [0.20,0.30) |
| Per-sentence doc-qualified citation enforcement | ✓ | R5-T2 strict gate + citation_completeness |
| NLI check sharing single audit write | ✓ | grep=1 grounded hit; mutation-tested |
| Faithfulness ≥ 0.50 | ⧗ DEFERRED | Code-ready; self-skips; blocked by missing keys |
| No common-path latency regression | ✓ | eval-db byte-identical; check_entailment ~0ms |

**Phase R5: code-complete. Does not fully close until make eval-live-r5 runs and
prints faithfulness ≥ 0.50 in a keyed env.**

## Carried-forward
- **⚠ Threshold tuning (IMMEDIATE — run before calling R5 done):** run
  `make eval-live-r5`; read `nli_entail_rate` and `disposition`. If answers are broadly
  withheld (low grounded count), lower `NLI_ENTAILMENT_THRESHOLD` to ~0.50–0.60 and
  re-run. One-line change in `config/settings/base.py`.
- **G5-live (faithfulness ≥ 0.50):** pending `LLM_API_KEY` + `RERANK_API_KEY` in env.
  Novita throttle is ready (2.1s, ≤29 batches/min); wall time ~10–12 min.
- **HYDE_ENABLED=True production flip:** pending live proof of HyDE benefit on the
  trigger-rate queries (K/145 from eval-live-r5); flip requires a 3rd Novita batch/query
  in the eval harness.
- **R5-T3 production LLM-backed entailment:** optional upgrade over token-overlap;
  out of R5 scope.
- **R6:** latency tuning (HNSW ef_search, prompt caching), frontend citation/entailment
  chips consuming `answer_structured`, audit completeness (`ai_decision_logs` field gaps).

## Process note
Adversarial verification: 7 skeptic passes + a completeness review, all PASS — every
pass completed cleanly (no retries). The single-write invariant was mutation-tested
independently (flipping `and`→`or` at policy_agent.py:160 makes an NLI-failing answer
return grounded, breaking exactly sub-case (d)); the inlined `_token_support` byte-identity
with `metrics.token_support` was confirmed by a 200k-case fuzz; layering (no ai_agents→evals
or backend edge) re-verified live.
