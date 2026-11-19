# Phase 6 · P6-T5 — Graceful Degradation  (Phase-6 Close-Out)

## Goal

Simulate LLM/OCR provider failure; confirm the deterministic fallback fires and the affected
fields are explicitly marked "needs review" — never a full-flow failure, never a silent bad value
(NFR-R1). Close Phase 6: prove the connected loop works browser-to-DB with guardrails + audit
intact under both success and failure (DoD); a scripted real-agent E2E run passes clean and faulted
(Checkpoint).

## What was done

- Built a reversible fault-injection harness at the `/services` interface seam:
  `FaultyLlmProvider(LocalLlmProvider)` and `FaultyOcrProvider(LocalOcrProvider)` raise
  `ProviderError(code=ERROR|TIMEOUT)` after a configurable `fail_after_n` pass-through count,
  so a specific mid-loop step can be faulted while earlier steps succeed. Only
  `get_llm_provider` / `get_ocr_provider` overrides flow faults through the **real** fallback
  code — no production adapter was edited.

- **Branch A — error + timeout (LLM + OCR):** every faulted call site → flow completes,
  deterministic result returned, "needs review" persisted, decision-log row records attempted
  route + failure + fallback:
  - OCR error/timeout → `ocr_status=failed`, `extracted_fields=None`; log `ocr_failed=true`,
    `route=deterministic`.
  - LLM gap-fill error/timeout → `ocr_status=processed`, all `field_confidence < 0.70`;
    log `needs_review=true`, `route=llm`.
  - LLM narrative error/timeout (travel + expense) → templated fallback, `degraded=true`;
    `AgentResult.status=needs_review`; log `degraded=true`, `route=deterministic`.
  - Error and timeout converge on the **same** `except ProviderError` handler — identical safe
    outcome (T5.2 proves this explicitly).

- **Branch B — reflection rejection:** unknown-image → LocalLlmProvider returns non-JSON →
  `LocationClaim(city="unknown")` → GroundingCritic with empty itinerary → `verdict="unsupported"`
  → `reflect()→_rejected()`. Assertions: `extracted_fields["location"]=None`,
  `field_confidence["location"]≤0.10`, `decision_log.reflection_outcome="rejected"`,
  `output_summary["needs_review"]=True`. The rejected hypothesis is **not** committed.

- **Branch C — low-confidence, non-failure:** coexists and is distinguishable from Branch A
  (`ocr_status=processed` not `failed`; `route=llm` not `deterministic`; no `ocr_failed` mark).
  Low-conf gap-fill is flagged but not an error; the decision log carries `needs_review=True`.

- **Atomicity (NFR-R2):** OCR fault leaves no partial commit — `ocr_status=failed`,
  `extracted_fields=None`, `processing_path=None`.

- **Idempotency (NFR-R3):** idempotent retry creates no duplicate receipt row; second
  `run_receipt_pipeline` on the same receipt updates the existing row.

- **No-silent-commit invariant (A5):** for every non-null committed field with
  `confidence < 0.70`, the decision log has `needs_review=True`. A fallback that commits
  without the mark is the failure mode; the A5 test enforces it.

- **Scripted real-agent E2E (Phase-6 Checkpoint):**
  - CLEAN run — plan → select → capture (airfare bytes, high-conf OCR) → assemble → approve.
    All 5 HTTP steps return expected status codes; lineage written; decision logs + audit intact.
  - FAULT-A run — `FaultyOcrProvider` at capture: `ocr_status=failed` in HTTP response body,
    `extracted_fields=None` in DB; Steps 4–5 complete (no 500); expense agent assembles empty
    set (unprocessed receipt) and returns `ok`.
  - FAULT-B run — `FaultyLlmProvider` at assemble (`explain_with_llm=True`): loop completes
    (no 500), `degraded=true` when narrative reached, `status=needs_review`.
  - Existing stub checkpoint (`test_checkpoint_e2e.py`) untouched and still green.

- **Zero production code changed.** The Phase-4 deterministic-fallback wiring held under
  every injected fault — this is the strongest possible NFR-R1 outcome, not a skipped task.

## Key decisions

- New real-agent scripted test module (not extending the stub test) so faults flow through
  actual fallback code rather than a mock that would only test the mock.
- Timeout simulated as `ProviderError(code="TIMEOUT")` — error and timeout share the call-site
  handler; the safe outcome is identical and proven by T5.2. Real `asyncio.wait_for` enforcement
  is deferred to Phase 7 (see Carried gaps).
- "Needs review" representation unchanged: `receipts.ocr_status='failed'`,
  `field_confidence < 0.70` + `field_provenance='ai_inferred'`, `ai_decision_logs.reflection_
  outcome='rejected'`, `output_summary->>'needs_review'/'degraded'`. No new column, no migration.
- Fault injection wraps the interface, never bypasses it — `FaultyLlmProvider` subclasses
  `LocalLlmProvider`; `FaultyOcrProvider` subclasses `LocalOcrProvider`. Production adapters
  byte-identical after the run.
- `_p6t5_support.py` imports `backend` inside function bodies only (`# noqa: PLC0415`); this is
  a test-helper reaching into backend to drive a real-agent E2E — acceptable and scoped.
  No module-level `import backend` exists anywhere in `ai_agents/`.

## Verification

A1–A8 passed; **17 tests green** (`TestBranchAError` ×4, `TestBranchATimeout` ×3,
`TestBranchBReflectionRejection` ×1, `TestBranchCCoexistence` ×2, `TestAtomicityIdempotency` ×3,
`TestScriptedE2ECleanRun` ×1, `TestScriptedE2EFaultOcr` ×1, `TestScriptedE2EFaultLlmNarrative` ×1,
`TestPhase6DoDCheckpoint` ×1); `ruff check` + `ruff format --check` + `mypy backend config` clean
(92 source files); drift guard empty (`git diff --stat database/` empty); no module-level
`import backend` in `ai_agents/`; no production source file modified; 3 new test files only.

## Carried gaps (→ Phase 7 / infra)

| Gap | Flagged in |
|---|---|
| **TIMEOUT ENFORCEMENT NOT WIRED** — `LLM_TIMEOUT_SECONDS`/`OCR_TIMEOUT_SECONDS` defined in `config/settings/base.py` but no `asyncio.wait_for` wrapper at any LLM/OCR call site; Phase-7 task = add enforcement (doc ref: `TECHNICAL_DESIGN` §timeout) | T5.2 + `_p6t5_support.py` module docstring |
| `expense_categorize_simple` no deterministic handler — gaps to the LLM in categorization | T4 |
| `correlation_id` column absent from `ai_decision_logs` | T4 |
| `LocalEmbeddings` per-process hash salting gives non-deterministic similarity scores across restarts | T4 |
| boto3/local-FS upload gap — receipt staged via ORM; live presigned-URL upload path untested | T1–T5 |
| Loosely-typed backend response schemas (return type widening) | T3 |
| NFR-SEC4 image serving (receipts served without auth-scoped URLs) | T1 |

---

## PHASE 6 — CLOSE-OUT SUMMARY  (all five sub-phases)

| Sub-phase | What was verified |
|---|---|
| **P6-T1** Frontend↔Backend | Live data sources, real states, auth-refresh seam; fixed login session establishment + Zustand-v5 render loop bug |
| **P6-T2** Backend↔AI agents | Delegation + scoped context + decision logs for all 6 agent paths; threaded + validated `context_ref_id` |
| **P6-T3** Database↔APIs | 62-test adversarial cross-role suite + lineage chain (receipt→expense→report→trip); tightened `context_ref_id` to record-level |
| **P6-T4** OCR/inventory/RAG | Three adapters through interfaces; M-R4 8/4 deterministic-first with 0 LLM calls on deterministic cases; fixed LocalOCR bytes-decode production bug |
| **P6-T5** Graceful degradation | NFR-R1 across all branches (A/B/C); atomicity + idempotency; real-agent scripted E2E CLEAN + FAULTED |

**Phase-6 DoD — MET.** The connected loop works browser-to-DB with guardrails + audit intact
under both success and failure.

**Phase-6 Checkpoint — GREEN.** Scripted E2E passes once clean and once with a mid-loop fault.

**Phase 7 may start.**
