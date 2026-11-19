# Phase 6 · P6-T4 — OCR & Inventory & RAG Integration

## Goal

Confirm the receipt pipeline, mock inventory, and policy retrieval work through their `/services`
adapters with deterministic/LLM routing intact; validate M-R4 reflects deterministic-first behavior.

## What was done

- Verified all three adapters reach work through their interfaces in dev/local mode (`OcrProvider`,
  `MockInventoryProvider`, `EmbeddingsProvider`/`PolicyRetrieval`), with a `SpyLlmProvider`
  wrapping the `LlmProvider` interface as the anti-cheat call-counter.
- M-R4 over a 12-input characterized set (routing writers: orchestrator + receipt_analysis):
  **8 deterministic / 4 llm (67% / 33%)**, deterministic-first invariant holds; every deterministic
  case = 0 LLM calls (per-input `spy.delta == 0`).
- Proved `route='llm'` ≠ "LLM called": silent policy Q&A and the deterministic travel path both
  log `route=llm` with 0 provider invocations — the label describes dispatch, not invocation.
- **OCR:** clean receipts → deterministic provenance, 0 LLM; low-confidence fixture → `ai_inferred`,
  grounded, reflection-checked, `route=llm`.
- **Inventory:** shortlist ⊆ seeded dataset (no fabrication); both in/out-of-policy polarities
  present; OOP never top-ranked; 0 LLM calls.
- **RAG:** grounded question → cited answer (≥2 LLM calls: hypothesizer + critic); off-topic
  question → "silent", 0 LLM calls (short-circuits before any provider call). Similarity threshold
  0.20 recorded.
- **Production fix:** `LocalOcrProvider.extract(bytes)` decode — bytes input produced an empty key,
  forcing every receipt to LLM gap-fill in local mode (NFR-P3 violation). One line, additive,
  zero blast radius.

## Key decisions

- Routing thresholds are implementation-calibrated (orchestrator 0.75 + `requires_reasoning`; OCR
  0.70; RAG 0.20); the docs mandate behavior, not numbers. No re-tune performed.
- M-R4 is a derived read over existing `ai_decision_logs.route` rows (UUID-snapshot + org scope),
  not new instrumentation.
- Spy wraps the `LlmProvider` interface (injected via `register_*(provider=spy)`), not a concrete
  adapter, so the call-counter covers every path to the model through all agents.

## Verification

A1–A11 passed. 8 tests green (`TestReceiptAdapter`, `TestInventoryAdapter`, `TestPolicyRagAdapter`,
`TestRouteMix`); `ruff check` + `ruff format --check` + `mypy` clean (91 source files); drift guard
empty (`database/` unchanged, no seed edit); `ai_agents` imports no `backend` (3 raw git-grep hits
were docstrings, not import statements).

## Carried gaps (out of P6-T4 scope)

- **`correlation_id` column absent on `ai_decision_logs`** — M-R4 scoped by UUID-snapshot + org
  filter; per-request M-R4 querying requires a `correlation_id` column (a migration — correctly
  not done here; named as an architecture follow-up).
- **`expense_categorize_simple` has no deterministic Cap-1 handler** — routes with 0 LLM calls
  (M-R4 claim holds) but returns `needs_review` (functional gap, not a routing defect).
- **`LocalEmbeddingsProvider` `hash()` is per-process salted** — grounded RAG determinism requires
  seed and query in the same interpreter (local feature-hash embedder property; retired by the
  real embeddings provider in production).
- **boto3 / local-FS upload gap** — seeded receipt images staged via ORM + `storage.put_object`;
  the live upload path (boto3 / real S3-compatible storage) remains untested.
- Loosely-typed backend schemas, NFR-SEC4 (hardened rate-limiting), and degradation behavior under
  fault injection carry forward to T5 / infra / a migration follow-up.
