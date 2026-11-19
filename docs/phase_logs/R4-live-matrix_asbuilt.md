# R4 Live-Matrix — As-Built

**Phase R4, final task.** Wire the OpenRouter embeddings adapter, verify `dimensions=1536`
pass-through, run the C0..C3 live matrix, make the B1/B2/B3/B4 adopt decision. Scope grew to a
reranker swap (HuggingFace → Novita) when HF's serverless endpoint proved unusable under load.
**Nothing committed** (commit prep is a separate step). Author: Garv Singh Saini.

## Outcome (one line)

FORK-J/K/L landed and the OpenRouter embedder path is **live-proven**, but the C0..C3 matrix could
**not** be completed — no reranker service sustained the ~775–930-call burst in acceptable time —
so the adopt decision is **B1 (retain), infrastructure-gated, not evidence-based**.

## FORK-J — OpenRouter embeddings adapter (new)

- `services/embeddings_provider/openrouter.py` — `OpenRouterEmbeddingsProvider`, distinct
  `EMBEDDINGS_PROVIDER="openrouter"` (mirrors the LLM-layer `openrouter` precedent, not a base_url
  overload of `"openai"`). httpx `POST {base}/embeddings`; reads the OpenRouter account from the
  generic `LLM_API_KEY` + `LLM_BASE_URL` (**zero new env vars**); prefixes the bare model with
  `openai/` in code; requests `dimensions=1536` (Fence 1) + shared L2-renorm + len guard.
- `services/embeddings_provider/_normalize.py` — shared `l2_normalize` extracted from real.py so
  both real (OpenAI-SDK) and openrouter (httpx) adapters reuse it (DRY, behaviour-identical;
  real.py refactor verified by its existing tests).
- Settings: `EMBEDDINGS_PROVIDER: Literal[...,"openrouter"]` (**additive**; default stays `local`).
  Factory + package `__init__` export the new adapter.

## FORK-K — `dimensions=1536` pass-through (live-verified, both models)

    [text-embedding-3-small] len=1536  L2=1.000000  -> PASS
    [text-embedding-3-large] len=1536  L2=1.000000  -> PASS   (native 3072 -> OpenRouter forwards dimensions=1536)

Confirmed OpenRouter forwards the Matryoshka `dimensions` param to OpenAI. Hard gate before the matrix.

## FORK-L — matrix runner wiring + two runner bugs fixed

- Skip-probe widened: `EMBEDDINGS_PROVIDER not in ("openai","openrouter")` skips.
- **Bug fixed:** `_apply_cell_env` hardcoded `EMBEDDINGS_PROVIDER="openai"` (overrode the make-target
  provider → never routed to the new adapter). Now honours the make-target value via a module-level
  capture.
- **Bug fixed:** the matrix omitted the `warmup()` call its sibling calibration fitters make → a cold
  reranker timed out the opening burst. Added a per-cell warmup (`hasattr`-guarded).
- Makefile live targets (`eval-live-matrix`, `calibrate-live`, `threshold-live`) →
  `EMBEDDINGS_PROVIDER=openrouter`.

## Reranker swap — HuggingFace → Novita (added; **HF kept as an inert skeleton**)

HF's free serverless Inference endpoint proved unusable at the matrix's sustained ~800-call burst
(HTTP 502 + persistent 60 s timeouts, ~95 min degrading). Swapped the production reranker to
Novita, which serves the **same** `baai/bge-reranker-v2-m3` behind a Cohere/Jina-style rerank API.

- `services/rerank_provider/novita.py` — `NovitaRerankProvider`, `RERANK_PROVIDER="novita"`, httpx
  `POST {base}/rerank`, Bearer, `{model,query,documents,top_n}` -> `{results:[{index,
  relevance_score|score}]}`. Reads `RERANK_API_KEY` from settings (never hardcoded, never logged).
  **429 exponential backoff** (1→2→4→8→16 s, bounded to 7 attempts) — self-throttles under Novita's
  RPM cap; on a sustained limit it fails safe to the R1 RRF order like the other adapters. 14 hermetic
  tests (incl. backoff-recover + bounded-failure). Live smoke: sub-second, correct top-1.
- Settings: `RERANK_PROVIDER: Literal[...,"novita"]` (**additive**; default stays `local`).
  Factory gains a lazy `novita` branch. **HuggingFace kept** (adapter, Literal value, `HF_API_TOKEN`,
  tests) as a swap-target skeleton — mirrors how `cohere.py` is kept.
- **Calibration carry-forward (key add, NOT recalibration):** `abstention_threshold.json` and
  `rerank_calibration.json` each gain a `novita` entry carrying HF's τ*=0.039299 and Platt (a,b) =
  (-2.469343, 3.580282). Rationale: identical model ⇒ identical sigmoid relevance scale ⇒ params
  transfer. Provenance recorded in each entry's `note`. The `huggingface` + `local` entries are
  untouched. Gate-read is unchanged: `retrieval.py` reads `load_abstention_threshold(RERANK_PROVIDER)`
  and `policy_retrieval.py` reads `load_calibration_params(RERANK_PROVIDER)` — at runtime value
  `"novita"` both now resolve. **Recalibrate on Novita after a successful live matrix** (carried-forward).

## Live matrix — attempted twice, not completed; B1 by deliberate choice

| Run | Reranker | Result |
|-----|----------|--------|
| 1 | HuggingFace | reached ~C3 at ~95 min, degraded under 502 + persistent 60 s timeouts |
| 2 | Novita | 90× HTTP 429 under the burst; ~385 s spent in backoff; 0 fallbacks (data would be valid) but no table by 34 min |

The runner is all-or-nothing (table + snapshot only after all 4 cells; partial results live in an
uncommitted in-process transaction — no partial table). Both runs abandoned.

**Decision — B1 (retain all embedder/prefix defaults).** The bar (stated before the run) requires a
*completed* reranked measurement; neither run produced one. Precise record: **live reranked
measurement infrastructure-gated by reranker-service rate limits; B1 by deliberate choice, NOT an
evidence-based no-gain.** The open R4-T1 question — *does the cross_document win survive into reranked
recall@5 on a semantic embedder?* — **remains unanswered.** B1 state: `EMBEDDINGS_PROVIDER=local`,
`EMBEDDINGS_MODEL=text-embedding-3-small`, `RAG_CONTEXTUAL_PREFIXES=False`, `RERANK_PROVIDER=local`
(CI default) — **zero production-default lines flipped**; `τ*=0.039299` unchanged.

## Verification (gates)

- **G1 ruff / format** — clean, all touched files.
- **G2 mypy** — clean, all touched modules.
- **G3 unit** — 11 OpenRouter-embeddings + 14 Novita hermetic tests; calibration/threshold/
  abstention-contract/rerank-provider tests green (HF entries + "huggingface" asserts intact);
  real.py `_normalize` refactor broke nothing.
- **G4 eval-db** — `47 passed, 15 deselected` (R3-byte-identical), re-run after every change incl. the
  `novita` key adds. Removing nothing; local path untouched.
- **G5 live matrix** — NOT produced (infrastructure-gated; readings above).
- **τ\*** — not recalibrated; `novita` key carries HF's τ* (carry-forward).
- **G9/G11** — alembic drift "No new upgrade operations detected"; no migration; every embedding
  len==1536 (FORK-K live-proven).

## Carried-forward

- **Live matrix + adopt decision** — one run away once a rate-limit-tolerant reranker path exists
  (higher Novita tier, or a proactive fixed-spacing throttle in the Novita adapter). Not blocking R5.
- **τ*/Platt recalibration on Novita** — recommended after the matrix runs; currently carried from HF
  (same model — defensible).
- **Production (Render) env** — set `RERANK_PROVIDER=novita`, add `RERANK_API_KEY` (Novita); the HF
  path remains available as a skeleton.
- **Rotate `HF_API_TOKEN`** — supplied via chat this session (standing rotation discipline).
- R5 (HyDE / citation enforcement / NLI); gate-8 latency (now Novita; same budget).
