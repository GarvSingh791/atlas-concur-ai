# P4-T10 — Expense Agent (FR-X1..X5)

**Sub-phase:** P4-T10
**Title:** Expense Agent — smart match, assemble, pre-submit check
**Date:** 2026-06-14
**Author:** Garv Singh Saini

---

## WHAT WAS BUILT

13 new files under `ai_agents/` — zero existing files modified.

| File | Responsibility |
|---|---|
| `ai_agents/agents/expense_agent/__init__.py` | Exports `ExpenseAgent` + `register_expense(registry, *, provider=None)` |
| `ai_agents/agents/expense_agent/intent.py` | `ExpenseIntent` dataclass + `parse_intent(payload)` |
| `ai_agents/agents/expense_agent/ingest.py` | Owner-scoped reads: gather receipts (`extracted_fields` as given; no OCR), card transactions, and trip segments for a trip/period |
| `ai_agents/agents/expense_agent/matching.py` | Deterministic entity resolution across card transaction ↔ receipt ↔ trip segment; duplicate detection (receipt + txn same charge → one entry); unmatched surfacing; idempotency guard (skip already-linked/already-matched) |
| `ai_agents/agents/expense_agent/categorize.py` | Deterministic category assignment (segment_type priority → expense_type → vendor keyword heuristics → 'other'); hotel-folio split (`is_itemized=true`; sibling entries share `sibling_group_id`; parts-sum invariant checked) |
| `ai_agents/agents/expense_agent/assemble.py` | Draft report assembly: `total_amount` derived from entries (never free-entered); trip linkage; mixed-currency detection |
| `ai_agents/agents/expense_agent/presubmit.py` | Policy evaluation per entry via Policy Evaluation tool; `pre_submit_flags` list with plain-language explanation + clause citation per flag; no silent in-policy false-pass (M-A3 assertion) |
| `ai_agents/agents/expense_agent/reflection.py` | Deterministic critic: catches mismatched-date/amount matches and silently-dropped policy flags; corrects mixed-currency report totals after adjustment |
| `ai_agents/agents/expense_agent/narrative.py` | Optional LLM flag/entry explanations (gated by `explain_with_llm`); `ProviderError` degradation to templated fallback + `needs_review` |
| `ai_agents/agents/expense_agent/persistence.py` | `persist_expense_report()`: writes `expense_reports` + `expenses` via `database.models` with `session.flush()` ONLY; links `receipts.expense_id` and `card_transactions.matched/expense_id`; stores `pre_submit_flags` snapshot; no commit |
| `ai_agents/agents/expense_agent/agent.py` | Full pipeline orchestration: ingest → match → categorize → itemize → assemble → pre-submit → reflect → explain → persist; decision log on every exit path; role-bound check; GuardrailBlocked caught (log still written) |
| `ai_agents/tests/test_expense_agent.py` | 43 hermetic tests (no DB, no LLM, no network); all 12 verification gates + extras |
| `ai_agents/tests/test_expense_agent_db.py` | Live-DB tests (`@pytest.mark.db`); self-skip without Postgres; fixture-extracted-receipts strategy |

---

## PIPELINE

9-step deterministic pipeline (LLM confined to step 8):

1. **INGEST** — read receipts (`ocr_status=processed`, non-null `extracted_fields` only; unprocessed surfaced, never silently dropped), card transactions (unmatched only), and trip segments via `ItineraryCalendarTool`. All reads scoped to org + owner (idempotency: skip `already_linked` receipts and `already_matched` txns).
2. **SMART MATCH** — match key `(round(amount,2), currency, normalized_vendor, date_bucket)`. Vendor normalization: token-containment with confidence penalty for partial match. Receipt + txn same charge → one `MatchCandidate` (`is_duplicate_resolved=True`). Unmatched items surfaced as advisory entries — nothing silently dropped.
3. **CATEGORIZE** — priority: matched `segment_type` → receipt `expense_type` → vendor keyword heuristics → `other`. No LLM.
4. **ITEMIZE** — receipt with ≥2 `line_items` → sibling `DraftEntry` rows sharing `sibling_group_id`, each `is_itemized=True`, each categorized independently. Parts-sum invariant enforced within $0.01; mismatch → `itemization_mismatch` blocking flag.
5. **ASSEMBLE** — `DraftReport` with `total_amount = round(sum(entries), 2)` (DERIVED, never input). `trip_id` only when all matched segments share one trip. Mixed currency → `needs_review`, no dishonest cross-currency sum.
6. **PRE-SUBMIT CHECK** — `build_eval_item(entry)` maps each entry to a typed `EvalItem` (C1: lodging uses per-night rate; C2: ground transport without known vehicle class evaluates as `receipt`; C3: `receipt_required` always in-policy — missing receipt derived agent-side). Every failing verdict → `policy_violation` flag with `source_clause_ref` citation. Skip verdict (`observed=={}`) → `not_evaluated` advisory (never silently treated as in-policy). M-A3 assertion: `count(policy_violation) >= count(failing verdicts)`.
7. **REFLECT** — `ExpenseMatchHonestyCritic` runs two checks: (a) match honesty — entry amount/date must align with linked segment; (b) no dropped failing verdict — recomputes expected flags from verdict index and re-injects any missing. Both pass → `supported`, `confidence=0.95`, assertion promoted. Empty report → skip reflection (trivial pass).
8. **EXPLAIN (LLM-gated)** — if `explain_with_llm` and provider present: `provider.complete()` adds narrative to each flag/entry explanation. `ProviderError` (or any exception) → templated fallback + `degraded=True`. `route=llm` only when narrative ran without degradation.
9. **PERSIST** — `persist_expense_report()`: `ExpenseReport(status=draft)` + `Expense` rows via `database.models`; `session.flush()` only; link `receipts.expense_id` and `card_transactions.matched=True/expense_id`; store `pre_submit_flags` snapshot. Never commit (deferred to P4-T13 endpoint). Append `ConversationMessage` if `conversation_id` supplied (non-fatal try/except). One `ai_decision_logs` row on every exit path.

---

## KEY DECISIONS

**Fixture-extraction sequencing** — Seeded receipts in the Phase 3 DB have `ocr_status='pending'` and `extracted_fields=NULL` because OCR/extraction is P4-T11, not yet built. Tests supply `Receipt` rows with `ocr_status=processed` and populated `extracted_fields` as fixtures in the rollback session. The agent consumes `extracted_fields` as given; it never runs OCR. Documented as an authorized sequencing decision (P4-T11 boundary respected), not a gap.

**Flush-only persistence (mirrors P4-T9)** — `persist_expense_report()` writes `database.models` directly with `session.flush()` ONLY; `status=draft`; commit + audit + the draft→submitted transition are deferred to the P4-T13 endpoint. Root cause: the backend expense service (`backend/app/modules/expense/service.py`) commits its own session and cannot store AI-derived fields (`pre_submit_flags`, `match_source`, `trip_segment_id`); `ai_agents` cannot import `backend` (layering law). Same finding as P4-T9's `create_trip`.

**LLM scope = deterministic core + optional explanations** — The entire decision path (match, categorize, itemize, assemble, pre-submit check, reflection) runs without a language model. LLM is invoked ONLY for flag/entry narrative when `explain_with_llm` is set; `ProviderError` → templated fallback + `needs_review` (route recorded as `llm` only when the model actually ran without degradation).

**No registry wiring** — `register_expense()` is defined and tested but is not called at application startup. Composition-root wiring (registering the agent in the live orchestrator) is P4-T13's responsibility, consistent with how `register_travel()` was handled in P4-T9.

---

## CORRECTNESS BUGS FOUND AND FIXED IN QA (before completion)

All three bugs were found and corrected during code quality review before any test run. No bugs survived to the validation suite.

1. **`agent.py`: stale `report.mixed_currency` reference** — The status-gate condition and output dict referenced the pre-reflection `report` object's `mixed_currency` field instead of `final_report.mixed_currency`. Would have raised `AttributeError` at runtime on any mixed-currency trip where reflection produced a corrected report object. ROOT CAUSE: intermediate variable renamed during refactor; reference not updated.

2. **`persistence.py`: value-equality vs identity bug in skipped-entry filtering** — Skipped-entry tracking used a `list` with `e not in skipped_entries`, which relies on `__eq__` for frozen dataclasses. Two `DraftEntry` instances with identical field values (e.g., two line items with the same amount) would both be excluded even if only one was meant to be skipped. Fixed by switching to `skipped_entry_ids: set[int]` tracking object identity via `id(e)`. ROOT CAUSE: Python's equality-vs-identity distinction for frozen dataclasses.

3. **`reflection.py`: wrong total when critic corrects a mixed-currency report** — After the critic removed a mismatched entry and rebuilt the report, `total_amount` was recalculated as `sum(e.amount for e in new_entries)` across all currencies, producing an incorrect (cross-currency) sum. Fixed by calling `assemble_report()` for mixed-currency reports so the corrected total respects the primary-currency-only summation rule. ROOT CAUSE: total recalculation after critic correction did not group by currency.

---

## VALIDATION EVIDENCE

| Check | Result |
|---|---|
| Hermetic tests | **43/43 passed** |
| Full suite (no DB) | **297 passed / 111 skipped / 0 failures / 0 regressions** |
| `ruff check` | Clean |
| `ruff format --check` | Clean |
| `mypy ai_agents/agents/expense_agent/` | Clean (11 source files) |
| Drift guard (`git status`) | Zero changes to tracked files — no migration, no model, no backend, no orchestrator |

Live-DB tests (`@pytest.mark.db`) self-skip without Postgres. To validate end-to-end persistence:
```
pytest -m db ai_agents/tests/test_expense_agent_db.py
```
Run against a seeded stack after P4-T13 wires the endpoint.

---

## STATUS

Phase 4 in progress. P4-T10 complete and committed.

Open items:
- Live-DB persistence gate against seeded stack (requires Postgres with Phase 3 seed data)
- P4-T13 endpoint wiring (register_expense() + request handler + commit)

Next sub-phase: **P4-T11 — Receipt Analysis pipeline** (OCR extraction + AI gap-fill + field provenance).
