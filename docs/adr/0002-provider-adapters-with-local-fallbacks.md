# 0002 — Provider adapters with deterministic local fallbacks

- **Status:** Accepted
- **Date:** 2026-06-13 (phase P4-T1); rerank and email adapters added later in the same pattern
- **Decider:** Garv Singh Saini

## Context

Atlas depends on several paid or networked services: an LLM, vision OCR, text embeddings, a cross-encoder reranker, S3-compatible object storage and email. Three needs pulled against each other:

1. CI and local development must run the **whole** stack (including the Playwright suite) with no API keys and no network, and give the same answer every time.
2. The staging demo must use real models so a visitor sees real OCR and real answers.
3. Switching a vendor (for example the LLM) should be a configuration change, not a code change, and a vendor outage should degrade the product, not crash it.

## Decision

Every external dependency sits behind an **interface in `services/`**, chosen at runtime by a `*_PROVIDER` setting, and every interface has a **deterministic offline implementation**:

| Adapter | Setting | Offline default | Real implementations |
|---|---|---|---|
| LLM | `LLM_PROVIDER` | `local` | `openrouter`, `anthropic` |
| OCR | `OCR_PROVIDER` | `local` | `openrouter` (vision) |
| Embeddings | `EMBEDDINGS_PROVIDER` | `local` | `openai`, `openrouter` |
| Rerank | `RERANK_PROVIDER` | `local` | `novita`, `cohere`, `huggingface` |
| Storage | `STORAGE_PROVIDER` | `local` (filesystem) | `minio` (any S3-compatible store, including Cloudflare R2) |
| Email | `EMAIL_PROVIDER` | `console` (delivers nothing) | `smtp`; `off` |

Rules that come with it:

- Each package has `interface.py` and `factory.py`; callers receive a provider object and never construct an HTTP client themselves.
- Real adapters initialise lazily: a missing key or SDK raises `ProviderError` at **call time**, never at import time, so hermetic CI always imports cleanly.
- `services/` imports only `config`. Adapters raise their own `ProviderError` hierarchy, never the web layer's errors.
- Callers treat `ProviderError` as a degradation signal: templated or deterministic output marked `needs_review` (agents), RRF order instead of a rerank (RAG), a saved receipt with fields left for the user (OCR).

## Alternatives considered

| Option | Why not |
|---|---|
| Call vendor SDKs directly and mock them in tests | Mocks drift from real behaviour, and the end-to-end suite would still need keys or a mocking layer at the HTTP level. A first-class local provider is used by real code paths in CI. |
| Record and replay HTTP fixtures | Works for unit tests, but not for interactive local development or the E2E lane, and fixtures go stale when prompts change. |
| A third-party LLM framework as the abstraction | Adds a large dependency for what is a handful of small interfaces, and hides the exact request that is sent, which matters for logging and cost control. |

## Consequences

**Positive**

- The full test pyramid (hermetic pytest, DB-backed pytest, eval-db, Playwright E2E) runs with `local` providers and no secrets, and is deterministic. The CI RAG regression floor (recall@10 0.819 on the deterministic path) can be gated on byte-identical output.
- Staging switches to OpenRouter (Gemini 2.5 Flash, fallback Gemini 2.5 Flash Lite) for LLM and OCR by environment variables only (`render.yaml`).
- Vendor failures degrade instead of erroring; for example a rerank failure falls back to RRF order.

**Negative / trade-offs**

- `local` results are not real quality. The OCR stub validates plumbing, not vision accuracy, and the published keyed RAG numbers (recall@10 0.959) come from offline runs, not from staging, which runs the local embedder and reranker to stay cheap.
- Each new capability needs two implementations to stay testable.
- A configuration mistake can silently select the local provider. Staging's choices are pinned in `render.yaml`, and the env audit (`scripts/ops/env_audit.py`, run in CI) fails when a provider switched on there has no secret declared for it.

## Evidence

- Factories: `services/llm_provider/factory.py`, `services/ocr_provider/factory.py`, `services/embeddings_provider/factory.py`, `services/rerank_provider/factory.py`, `services/storage_provider/factory.py`, `services/email_provider/factory.py`.
- Tests: `services/tests/test_llm_provider.py`, `test_ocr_provider.py`, `test_embeddings_provider.py`, `test_rerank_provider.py`, `test_storage_provider.py`, `test_email_provider.py`; degradation in `ai_agents/tests/test_openrouter_degradation.py` and `ai_agents/tests/test_p6_t5_degradation_db.py`.
- Settings: `config/settings/base.py` (`*_PROVIDER` fields); staging values in [`render.yaml`](../../render.yaml).
- Phase log: [`P4-T1_provider-abstractions.md`](../phase_logs/P4-T1_provider-abstractions.md). RAG rerank fallback: [ADR-003](../RAG/adr/ADR-003-cross-encoder-reranking.md).
