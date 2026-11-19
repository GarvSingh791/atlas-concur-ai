# P4-T11 — Receipt Analysis Pipeline
**Sub-phase:** P4-T11  
**Title:** Receipt Analysis Pipeline (OCR Core, Gap-Fill, Reflection)  
**Date:** 2026-06-14  
**Author:** Garv Singh Saini

---

## What Was Built

Seven new files under `ai_agents/receipt_pipeline/` — zero existing files modified.

| File | Role |
|------|------|
| `__init__.py` | Public surface: `run_receipt_pipeline`, `apply_user_correction`, re-exported dataclasses |
| `core.py` | Deterministic OCR core: resolves image bytes via `StorageProvider` (never exposes raw URL downstream); calls `OcrProvider`; per-field confidence routing at 0.70 threshold; returns `RoutingDecision` (high-confidence fields done; low-confidence / absent fields flagged for gap-fill) |
| `gap_fill.py` | LLM gap-fill: date-window itinerary filter via Itinerary/Calendar tool; PII-minimal prompt (partial fields + itinerary context only; raw image bytes never sent to model); parse inferred fields into `GapFillResult`; `ProviderError` → affected fields null + `'ai_inferred'` provenance + `needs_review`; deterministic fields already resolved are preserved |
| `reflection.py` | `ReceiptFieldHypothesizer` + `build_grounding_facts`; reuses reference `GroundingCritic` from P4-T3; Paris-paradox defence scoped to the `location` field: critic checks inferred location against itinerary city/region and revises to null-for-review when the inference contradicts the grounding context |
| `correction.py` | `apply_user_correction()`: org+user scoped receipt load; sets `extracted_fields[field]` to corrected value; flips `field_provenance[field]` to `'user_edited'`; no other fields touched; `session.flush()` only |
| `pipeline.py` | Pipeline orchestrator: `ocr_status` pending → processed/failed lifecycle; routes to deterministic or `agent_gap_fill` path; maps reflection outcomes back to `field_provenance`; writes one `write_decision_log` row per run; UPDATE existing receipts row (never insert); `session.flush()` only |
| `tests/test_receipt_pipeline.py` | 52 hermetic tests covering all 12 plan break-tests plus PII safety and decision-log count |

---

## Pipeline

**6-stage Routing Pattern:**

1. **Ingest** — image bytes resolved via `StorageProvider`; `ocr_status=pending` at start
2. **Deterministic OCR core** — `OcrProvider`; per-field confidence at 0.70 threshold; high-confidence fields done, `field_provenance='deterministic'`
3. **Confidence routing** — ALL high-confidence → `processing_path='deterministic'`, stop, no LLM; any low/absent → `agent_gap_fill` path
4. **Agentic gap-fill** — Itinerary/Calendar tool grounding; PII-minimal LLM prompt; infer missing fields; `field_provenance='ai_inferred'`; `grounding_context` stored
5. **Reflection** — `ReceiptFieldHypothesizer` + `GroundingCritic`; Paris-paradox defence on location; critic rejection → null + `needs_review`; `reflection_outcome` recorded
6. **Write results** — UPDATE existing receipts row: `extracted_fields`, `field_confidence`, `field_provenance` per-field, `grounding_context`, `ocr_status='processed'`, `processing_path`; flush-only; commit deferred to P4-T13

---

## Key Decisions

**Confidence threshold = 0.70** — chosen as the routing gate between the deterministic and agent paths. Fields at or above 0.70 are treated as resolved by the OCR core; fields below or absent are routed to gap-fill. Threshold is a named constant, not a magic number, so it can be tuned without touching logic.

**Paris-paradox defence scoped to location field** — the research's canonical reflection case; the critic compares the inferred location against the itinerary city/region. Other fields use the same hypothesizer/critic scaffold but the location check is the primary grounding defence. Documented explicitly so Phase-5 or future evaluations know the current critic scope.

**GroundingCritic reused from P4-T3** — no new critic class was built; `ReceiptFieldHypothesizer` wraps the extracted + inferred fields into the existing `reflect()` contract. Keeps the critic implementation single-source.

**PII minimization enforced in gap_fill.py** — only partial field values + date-windowed itinerary context are passed to the LLM; raw image bytes are resolved once by `StorageProvider` in `core.py` and never forwarded to downstream stages or logged (NFR-SEC4, NFR-SEC5).

**UPDATE not INSERT** — the pipeline overwrites the existing receipts row (seeded stubs from P3-T5); a re-run is idempotent (row count stable). The insert-focused patterns from P4-T9/T10 do not apply here; this is the only pipeline stage that updates a pre-existing record rather than creating one.

**Pre-existing mypy errors in services/ocr_provider/local.py (2 errors from P4-T1)** are out of scope for P4-T11 and were NOT introduced by this task. Confirmed by running mypy scoped to `ai_agents/receipt_pipeline/` — zero errors. The P4-T1 errors are logged here for visibility; they should be addressed when the real OCR provider is wired in P8.

---

## Validation Evidence

- **52/52 hermetic tests passed** covering all 12 plan break-tests:
  - deterministic clean path
  - route mix (decision log route='deterministic' vs 'llm')
  - gap-fill triggered (Itinerary/Calendar tool called with correct user_id + date window)
  - Paris paradox (location revised; reflection_outcome='revised')
  - reflection pass + revised both recorded
  - provenance completeness (every extracted field has a provenance entry)
  - user-edit flip (field_provenance='user_edited'; no other fields touched)
  - idempotent re-run (receipts row updated, not duplicated; row count stable)
  - OCR degradation (ocr_status='failed'; no partial corrupt write)
  - LLM degradation (ProviderError → deterministic fields preserved; affected fields null + needs_review)
  - decision log (exactly one row per run; agent='receipt_analysis'; route, tools_used, confidence aggregate, reflection_outcome present)
  - flush-only rollback (session.commit() never called in pipeline)
  - PII safety (raw image bytes absent from decision log, grounding_context, and all LLM inputs)
  - decision-log count

- **298 existing tests pass / 0 regressions**
- **ruff + format + mypy (scoped to ai_agents/receipt_pipeline/) clean**
- **Drift guard empty** — no migration, no model, no backend, no orchestrator change
- **No backend/ imports** confirmed

---

## Status

Phase 4 in progress. **P4-T11 complete.**

**Open items:**
- Live-DB pipeline run against seeded receipts (`pytest -m db`)
- P4-T13 endpoint wiring (upload trigger + PATCH user-correction + commit + audit)
- Pre-existing P4-T1 mypy errors in `services/ocr_provider/local.py` to be resolved at P8 when a real OCR provider is wired

**Next:** P4-T12 — Approval Agent
