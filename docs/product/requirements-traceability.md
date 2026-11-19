# Requirements traceability: PRD → as-built

This matrix takes every requirement in the original [PRD](PRD.md) and records what was actually built, with evidence you can open: a code path, a test, or a phase log. It is for reviewers who want to check scope claims rather than take them on trust. Where the code and the original spec disagree, the code wins and the row says "original design vs as-built".

**Related:** [PRD.md](PRD.md) · [concur-to-atlas-map.md](concur-to-atlas-map.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) · [TESTING.md](../quality/TESTING.md) · [phase logs](../phase_logs/README.md)

> [!NOTE]
> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.

## Summary

| Group | Items | Shipped | Shipped (changed) | Partial | Deferred | Out of scope |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| MVP scope (MVP-01..09) | 9 | 8 | 1 | 0 | 0 | 0 |
| User stories (US-E1..M3..F3) | 11 | 7 | 3 | 1 | 0 | 0 |
| Functional requirements (FR-T, X, R, A, P) | 28 | 24 | 4 | 0 | 0 | 0 |
| Non-functional requirements (NFR-S, SEC, P, R) | 18 | 11 | 4 | 2 | 1 | 0 |
| Success metrics (M-T, M-A, M-R) | 10 | 3 | 1 | 2 | 4 | 0 |
| Out-of-scope list (OOS-01..09) | 9 | 0 | 0 | 0 | 0 | 9 |
| **Total** | **85** | **53** | **13** | **5** | **5** | **9** |

In short: every MVP feature and functional requirement was built, and every user story except one acceptance criterion. The gaps are that criterion (a viewer for AI decision rows), background processing for AI work, a formal latency measurement, at-rest encryption beyond what the hosting providers supply, and the time-saved and some automation metrics, which were never instrumented. Every out-of-scope item stayed out. A large amount was built beyond the PRD in phases 9 to 12 ([see below](#built-beyond-the-original-prd)).

**Status key**

| Status | Meaning |
|---|---|
| Shipped | Built as specified, with tests |
| Shipped (changed) | Built, but the as-built design differs from the original wording; the note says how |
| Partial | Some acceptance criteria or parts are met; the note says what is missing |
| Deferred | Not built or not measured; recorded as future work |
| Out of scope | Deliberately not built (from the PRD's out-of-scope list) |

Code and test paths are relative to the repository root. Test counts for the whole project are in the [README](../../README.md#quality--evaluation) (hermetic pytest 4,225 passed, DB-backed 2,069 passed, Vitest 968 tests in 133 files, 80 `/api/v1` endpoints in the contract matrix, `eval-db` 48 tests).

---

## 1. MVP scope

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| MVP-01 | Authentication and RBAC for three roles, role-scoped data | Shipped | `backend/app/core/rbac.py`, `backend/app/modules/identity/`, `backend/tests/test_rbac.py`, `backend/tests/test_record_scoping.py`, [P2-T3](../phase_logs/P2-T3_auth_system.md), [P2-T4](../phase_logs/P2-T4_rbac_record_scoping.md) | Roles: employee, manager, finance admin. Permission keys `resource.action.scope` |
| MVP-02 | Conversational travel planner over mock inventory | Shipped | `ai_agents/agents/travel_agent/`, `services/mock_inventory/`, `ai_agents/tests/test_travel_agent.py`, [P4-T9](../phase_logs/P4-T9_travel_agent.md), [P5-T5](../phase_logs/Phase5_P5-T5.md) | Flights and hotels; editable intent chips added during UAT |
| MVP-03 | Policy engine: deterministic rules + grounded policy Q&A with citations | Shipped | `ai_agents/tools/policy_eval.py`, `ai_agents/agents/policy_qa.py`, `ai_agents/rag/`, `ai_agents/tests/test_tool_policy_eval.py`, `ai_agents/tests/test_policy_agent.py`, [P4-T8](../phase_logs/P4-T8_policy_agent.md), [RAG docs](../RAG/README.md) | 24 active rules enforcing 29 clauses today; Q&A runs on a four-document corpus (see NFR and beyond-PRD rows) |
| MVP-04 | Receipt scanner: extraction → confidence routing → grounded gap-fill → editable result | Shipped | `ai_agents/receipt_pipeline/`, `ai_agents/receipt_pipeline/tests/test_receipt_pipeline.py`, [P4-T11](../phase_logs/P4-T11_receipt_pipeline.md), [P5-T6](../phase_logs/Phase5_P5-T6.md) | |
| MVP-05 | Expense automation: entries from receipts and trip context, smart match, draft report | Shipped | `ai_agents/agents/expense_agent/`, `ai_agents/tests/test_expense_agent.py`, `ai_agents/tests/test_expense_agent_db.py`, [P4-T10](../phase_logs/P4-T10_expense_agent.md) | |
| MVP-06 | Single-level approval routing with auto-approval and escalation | Shipped (changed) | `ai_agents/agents/approval_agent/`, `backend/app/modules/approval/`, `ai_agents/tests/test_approval_agent.py`, [P4-T12](../phase_logs/P4-T12_approval_agent.md), [phase 12 grades](../phase_logs/phase_12_followups_grades.md) | Still one deciding approver. Original design vs as-built: routing also falls back to a finance admin when the manager is missing or is the submitter, and flags that need an SVP or VP route to an approver of that grade |
| MVP-07 | Role-aware dashboard with three role views | Shipped | `backend/app/modules/analytics/`, `frontend/app/(app)/dashboard/page.tsx`, `backend/tests/test_analytics_api.py`, [P5-T4](../phase_logs/Phase5_P5-T4.md), [P5-T9](../phase_logs/P5-T9_analytics_policy.md) | |
| MVP-08 | Audit trail for state changes and AI decisions | Shipped | `backend/app/core/audit.py`, `ai_agents/decision_log/`, `backend/tests/test_audit.py`, `ai_agents/tests/test_decision_log_writer.py`, [P4-T2](../phase_logs/P4-T2_decision_logging_guardrails.md) | `audit_log` for business events, one `ai_decision_logs` row per AI task |
| MVP-09 | AI guardrails: routing, reflection/critic, grounding | Shipped | `ai_agents/orchestrator/classifier.py`, `ai_agents/guardrails/`, `ai_agents/tests/test_orchestrator_classifier.py`, `ai_agents/tests/test_reflection_loop.py`, `ai_agents/tests/test_citation_contract.py`, [P4-T3](../phase_logs/P4-T3_reflection_critic.md), [P4-T7](../phase_logs/P4-T7_intent_classification_routing.md) | |

**Simplifying assumptions (not counted above)**

| Assumption | As-built |
|---|---|
| Mocked travel inventory | Held: static fixtures plus deterministic synthetic options for any date (`services/mock_inventory/synthetic.py`) |
| Simulated card transactions and calendar | Held: seeded `card_transactions` and `calendar_events` |
| Single tenant, one fictional company | Original design vs as-built: every business table carries `organization_id` and login is organisation-aware ([phase 10 identity](../phase_logs/phase_10_known_limits_identity.md)); the demo still runs one seeded organisation |
| One policy document, small rule set | Original design vs as-built: one policy plus FAQ, How-To and Quick Reference documents (219 units) for RAG; 24 active structured rules |

---

## 2. User stories

| ID | Story | Status | Evidence | Note |
|---|---|---|---|---|
| US-E1 | Conversational trip planning | Shipped | `ai_agents/agents/travel_agent/`, `ai_agents/tests/test_travel_intent_extraction.py`, `ai_agents/tests/test_travel_selection_upsert.py`, `frontend/app/(app)/planner/page.tsx` | AC1–AC5 met. Selecting options adds them to the conversation's one draft trip; relative and vague dates ("a fortnight") supported ([phase 9 fxcard](../phase_logs/phase_9_product_gaps_fxcard.md)) |
| US-E2 | Policy questions answered with sources | Shipped | `ai_agents/agents/policy_agent.py`, `ai_agents/agents/policy_qa.py`, `ai_agents/guardrails/citations.py`, `ai_agents/tests/test_r3_abstention_contract.py`, `ai_agents/tests/test_citation_contract.py` | Citations must point at a clause that was actually retrieved; when the policy is silent the assistant says so and names an escalation contact, with no LLM call |
| US-E3 | Capture a receipt with minimal effort | Shipped | `ai_agents/receipt_pipeline/`, `backend/app/modules/receipt/router.py`, `backend/tests/test_receipts_api.py`, `backend/tests/test_c7r_18_receipt_review_api.py` | Six fields extracted (`amount`, `currency`, `date`, `vendor`, `location`, `expense_type`); inferred fields marked `ai_inferred`; inline correction marks them `user_edited`; a draft expense is created and linked automatically |
| US-E4 | Auto-assembled expense report | Shipped | `ai_agents/agents/expense_agent/` (`matching.py`, `categorize.py`, `assemble.py`, `presubmit.py`), `ai_agents/tests/test_presubmit_justification.py` | Itemised receipts (for example a hotel folio) with two or more line items become sibling entries |
| US-E5 | Track status and reimbursement | Shipped (changed) | `backend/app/modules/lifecycle/`, `backend/app/modules/approval/lifecycle.py` (`owner_approval_info`), `backend/tests/test_pg_lifecycle_db.py`, [phase 9 lifecycle](../phase_logs/phase_9_product_gaps_lifecycle.md) | Original design vs as-built: nothing moved a report to `reimbursed` until phase 9 added the finance reimburse action. There is no separate "returned" status: a returned report goes back to `draft`, and the owner sees the reviewer's reason |
| US-M1 | Fast, contextual approvals | Shipped | `backend/app/modules/approval/service.py`, `backend/tests/test_approvals_api.py`, `backend/tests/test_approvals_read_api.py`, [P5-T8](../phase_logs/P5-T8_approval_dashboard.md) | Queue ordered by status, then blocking flags, then age; exception cards show clause citations; a reason is required to deny |
| US-M2 | Auto-approval of low-risk items | Shipped | `ai_agents/agents/approval_agent/routing.py`, `backend/tests/test_c7r_p12_report_approval_status.py` (`test_human_reversal_of_auto_approved_report`) | Auto-approval only for in-policy, zero blocking flags, no required approver, known total ≤ $500; rationale stored on the step. A human can reverse it until the report is reimbursed |
| US-M3 | Team spend visibility | Shipped (changed) | `backend/app/modules/team/`, `backend/tests/api/test_pg_team_view.py`, `backend/tests/test_c7_14_manager_dashboard_scope.py`, [phase 9 team](../phase_logs/phase_9_product_gaps_team.md) | The dashboard widget shipped in phase 5; a full Team view (open trips, reports by status, month-to-date and 90-day spend, drill-down) was added in phase 9 |
| US-F1 | Policy configuration | Shipped (changed) | `ai_agents/agents/policy_authoring.py`, `backend/app/modules/policy/history.py`, `backend/tests/test_issue5_rule_authoring_api_db.py`, `backend/tests/test_pg8_history_changes.py`, `backend/tests/test_fu_policy_document_db.py` | AC1–AC2 met (rules drafted from plain language stay `draft` until a human activates them). Original design vs as-built for AC3: the policy document has a `version`; individual rules are not versioned rows, their changes are recorded in the audit log and shown as per-rule history |
| US-F2 | Audit and exception review at scale | Partial | `backend/app/modules/approval/filters.py`, `backend/app/modules/audit/`, `backend/tests/api/test_pg_finance_filters.py`, `backend/tests/api/test_pg_csv_export.py`, `backend/tests/test_audit_api.py` | AC1 met (approvals filter by exception type, status, department, owner and dates; CSV export). AC3 met (decisions and auto-approval reversals carry a recorded reason). AC2 partly: state-change history is visible, and receipts show per-field provenance and confidence, but `ai_decision_logs` rows are stored without a screen to browse them |
| US-F3 | Spend and automation analytics | Shipped | `backend/app/modules/analytics/service.py`, `backend/tests/test_pg_integ_analytics_db.py` | KPIs: Consolidated Spend, Automation Rate, Exception Rate, Avg Cycle Time (submitted → reimbursed), Avg Time to Decision, Budget Posture; department budget vs actual |

---

## 3. Functional requirements

### Travel planning

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| FR-T1 | Parse free text into structured trip intent | Shipped | `ai_agents/agents/travel_agent/intent.py`, `extraction.py`, `ai_agents/tests/test_travel_intent_extraction.py`, `ai_agents/tests/test_pg8_vague_relative_dates.py` | |
| FR-T2 | One focused clarifying question per turn | Shipped | `ai_agents/agents/travel_agent/intent.py`, `ai_agents/tests/test_travel_agent_multiturn_db.py` | Returns exactly one question for the first missing slot |
| FR-T3 | Retrieve options from the mock inventory | Shipped | `ai_agents/tools/inventory.py`, `services/mock_inventory/`, `ai_agents/tests/test_tool_inventory.py`, `services/tests/test_mock_inventory.py` | |
| FR-T4 | Rank on price, compliance, preference, sustainability | Shipped | `ai_agents/agents/travel_agent/ranking.py` | Composite score: +100 for in-policy, plus inverse-price, sustainability and preferred-supplier bonuses, so compliance outranks price; out-of-policy options are shown, never hidden |
| FR-T5 | Label in/out of policy with reasons | Shipped | `ai_agents/tools/policy_eval.py`, `ai_agents/tests/test_tool_policy_eval.py`, `ai_agents/tests/test_tool_policy_eval_cabin_reason.py` | |
| FR-T6 | Shortlist with plain-language rationale | Shipped | `ai_agents/agents/travel_agent/narrative.py`, `ai_agents/agents/travel_agent/reflection.py`, `ai_agents/tests/test_travel_agent.py` | Templated explanation per option; an optional LLM narrative falls back to the templates on provider error. An LLM-free honesty critic checks every in-policy label against the policy verdict |
| FR-T7 | Persist trip, segments and initial workflow state | Shipped | `ai_agents/agents/travel_agent/persistence.py`, `ai_agents/tests/test_travel_selection_upsert_db.py` | Trip created as `draft` |

### Expense creation

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| FR-X1 | Entries from receipts, simulated card transactions and trip context | Shipped | `ai_agents/agents/expense_agent/ingest.py`, `ai_agents/tests/test_expense_agent_db.py` | Unprocessed receipts are surfaced, never dropped |
| FR-X2 | Smart match; surface duplicates and unmatched items | Shipped | `ai_agents/agents/expense_agent/matching.py`, `evals/datasets/m_a2_match.jsonl` | M-A2 F1 1.0 on 65 gold links |
| FR-X3 | Auto-categorise and itemise | Shipped | `ai_agents/agents/expense_agent/categorize.py`, `categorize_handler.py`, `ai_agents/tests/test_expense_categorize_rules.py`, `ai_agents/tests/test_expense_categorize_handler.py` | The expense agent's categoriser is rule-based with no LLM; the separate simple-categorise handler makes one LLM call only when rule confidence is below 0.80 |
| FR-X4 | Assemble a draft report | Shipped | `ai_agents/agents/expense_agent/assemble.py` | Foreign-currency lines converted into the report currency |
| FR-X5 | Pre-submit policy check with explanations | Shipped | `ai_agents/agents/expense_agent/presubmit.py`, `ai_agents/tests/test_presubmit_from_fields.py`, `ai_agents/tests/test_presubmit_justification.py` | A blocking flag stops submission. For justifiable rules (for example caps, per diems, preferred suppliers) a written justification makes the flag advisory so the report can be submitted and the approver decides; missing receipts and prohibited categories stay blocking |

### Receipt upload

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| FR-R1 | Accept images, store, link to an entry | Shipped (changed) | `backend/app/modules/receipt/router.py`, `backend/app/modules/receipt/upload_guard.py`, `backend/tests/test_ps_upload_guard.py`, `backend/tests/test_pdf_receipts.py` | Original design vs as-built: PNG, JPEG and WebP are type-sniffed by magic bytes and stripped of metadata (pixels are not re-encoded); PDFs are also accepted but only rasterised (first 3 pages to one image), never stored |
| FR-R2 | Deterministic extraction with per-field confidence | Shipped | `ai_agents/receipt_pipeline/core.py`, `services/ocr_provider/` | |
| FR-R3 | Route low-confidence fields to gap-fill | Shipped | `ai_agents/receipt_pipeline/core.py`, `ai_agents/receipt_pipeline/tests/test_receipt_pipeline.py` | Threshold 0.70; fields at or above it never reach the LLM |
| FR-R4 | Ground inferences in itinerary and calendar | Shipped | `ai_agents/receipt_pipeline/gap_fill.py`, `ai_agents/tools/itinerary_calendar.py`, `ai_agents/tests/test_tool_itinerary_calendar.py` | ±1-day window; image bytes are never sent to the LLM |
| FR-R5 | Provenance per field and inline correction | Shipped | `ai_agents/receipt_pipeline/correction.py`, `backend/tests/test_c7r_18_receipt_review_api.py` | `deterministic` / `ai_inferred` / `user_edited`. A receipt a reviewer relied on is locked (`409 RECEIPT_LOCKED`) |

### AI decision making

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| FR-A1 | One orchestrator classifies and routes tasks | Shipped (changed) | `ai_agents/orchestrator/`, `ai_agents/tests/test_orchestrator_classifier.py`, `ai_agents/tests/test_orchestrator_router.py` | Rule-based classifier with no LLM call; deterministic when confidence ≥ 0.75. Original design vs as-built: receipt uploads run through the receipt pipeline's own per-field routing rather than the orchestrator |
| FR-A2 | Tool calls for ground truth | Shipped | `ai_agents/tools/` (`inventory.py`, `policy_eval.py`, `policy_retrieval.py`, `profile.py`, `itinerary_calendar.py`), `ai_agents/tests/test_tool_profile.py` | |
| FR-A3 | Reflection/critic before commit | Shipped | `ai_agents/guardrails/reflection.py`, `ai_agents/receipt_pipeline/reflection.py`, `ai_agents/agents/travel_agent/reflection.py`, `ai_agents/tests/test_reflection_loop.py` | A rejected inference is nulled and marked `needs_review` |
| FR-A4 | Grounding/citations and confidence | Shipped | `ai_agents/guardrails/citations.py`, `ai_agents/rag/support.py`, `ai_agents/tests/test_c7r_sentence_support.py` | Each answer sentence must cite a retrieved clause and be supported by it |
| FR-A5 | Log every AI decision | Shipped (changed) | `ai_agents/decision_log/`, `ai_agents/tests/test_decision_log_writer.py` | One row per task. Original design vs as-built: inputs and outputs are stored as minimised summaries (secrets and PII removed), not verbatim |
| FR-A6 | Swappable model provider | Shipped | `services/llm_provider/factory.py`, `services/tests/test_llm_provider.py`, `services/tests/test_openrouter_provider.py`, [P4-T1](../phase_logs/P4-T1_provider-abstractions.md) | `LLM_PROVIDER`: `local`, `openrouter`, `anthropic`; staging uses OpenRouter → Gemini 2.5 Flash |

### Approval flow

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| FR-P1 | Route to the submitter's manager | Shipped (changed) | `backend/app/modules/approval/lifecycle.py`, `backend/app/modules/approval/grade_routing.py`, `backend/tests/test_fu_svp_routing_unit.py`, `backend/tests/test_fu_svp_approval_level_db.py` | Plus a finance-admin fallback and SVP/VP grade routing; a lower-grade decision is refused with `403 INSUFFICIENT_APPROVER_GRADE` |
| FR-P2 | Auto-approve low-risk, in-policy items with rationale | Shipped | `ai_agents/agents/approval_agent/routing.py`, `backend/tests/unit/test_unit_approval_determinism.py` | No LLM in the approval agent; it never auto-rejects |
| FR-P3 | Escalate exceptions with reasons and context | Shipped | `backend/app/modules/approval/risk.py`, `backend/tests/test_approval_risk.py` | |
| FR-P4 | Approve / reject with reason / request changes | Shipped | `backend/app/modules/approval/schemas.py` (`ApprovalDecisionRequest`), `backend/tests/test_approvals_api.py` | |
| FR-P5 | Every transition as an immutable audit event | Shipped | `backend/app/core/audit.py`, `database/models/approval_step.py`, `backend/tests/test_audit.py` | Immutability is by convention (see NFR-R4) |

---

## 4. Non-functional requirements

| ID | Requirement | Status | Evidence | Note |
|---|---|---|---|---|
| NFR-S1 | Modular monolith with clean module boundaries | Shipped | `backend/app/modules/`, `ai_agents/tests/test_no_backend_import.py`, [ADR 0001](../adr/0001-modular-monolith.md) | Dependencies point one way: `services` ← `ai_agents` ← `backend` |
| NFR-S2 | Stateless app layer; identity in tokens | Shipped (changed) | `backend/app/core/security.py`, `backend/app/core/rate_limit.py`, `backend/tests/test_rl_redis_store.py` | Signed JWT access (15 min) and refresh (7 days) tokens; no server-side session store. Rate-limit counters are per-process unless `RATE_LIMIT_STORE=redis`; staging uses the in-memory store |
| NFR-S3 | Long AI tasks run asynchronously | Deferred | `backend/app/modules/ai/streaming.py` | AI runs synchronously inside the request; there is no job queue (README known limitations) |
| NFR-S4 | Multi-tenant-ready data model | Shipped (changed) | `organization_id` on business tables, `backend/tests/test_kl_org_scoped_login_db.py`, [phase 10 identity](../phase_logs/phase_10_known_limits_identity.md) | Went further than a seam: org-scoped queries and org-aware login; RAG tenant-isolation eval returns 0 cross-tenant chunks in 27 cases. No database row-level security |
| NFR-SEC1 | Auth on non-public endpoints; short-lived tokens | Shipped | `backend/tests/test_identity_auth.py`, `backend/tests/api/test_contract_matrix.py` | Contract matrix checks 401 without a token on 80 endpoints |
| NFR-SEC2 | Server-side RBAC on every request | Shipped | `backend/app/core/rbac.py`, `backend/tests/api/test_contract_matrix.py` | Matrix checks 403 for a forbidden role |
| NFR-SEC3 | Record-level scoping | Shipped | `backend/app/core/rbac.py` (`scope_query`), `backend/tests/test_record_scoping.py`, `backend/tests/api/test_rbac_scoping.py` | Filters can only narrow a scope |
| NFR-SEC4 | Receipts and personal data protected at rest and in transit | Partial | `backend/app/modules/receipt/upload_guard.py`, `backend/tests/api/test_receipt_image_access.py`, [security model](../design/security-model.md) | Met: role-scoped signed URLs (300 s expiry by default), object keys only in the database, EXIF/GPS stripped, HTTPS on the hosted demo. Not done in the app: encryption at rest is left to the managed database and object store |
| NFR-SEC5 | AI logging without leaking secrets; minimal model inputs | Shipped | `ai_agents/decision_log/minimizer.py`, `ai_agents/receipt_pipeline/gap_fill.py` | Gap-fill prompts carry only partial fields and itinerary context |
| NFR-SEC6 | Guardrails against prompt-injected actions | Shipped | `ai_agents/guardrails/actions.py`, `ai_agents/tests/test_role_bound_check.py` | Fixed action allow-list tied to permission keys |
| NFR-P1 | Sub-second CRUD and dashboard reads | Partial | `backend/tests/test_p3_indexes.py` | Indexed queries are in place, but no load test or latency budget was measured for CRUD reads. Measured only for retrieval (fused hybrid p50 3.9 ms on the local path, [RAG metrics](../RAG/metrics/FINAL_METRICS_SUMMARY.md)) |
| NFR-P2 | Streamed conversational responses | Shipped (changed) | `backend/app/modules/ai/streaming.py`, `backend/tests/test_conversations_api.py` | Original design vs as-built: Server-Sent Events frames (`status`, `tool_activity`, `result`, `narrative`, `done`) are sent after the orchestrator finishes, not token by token |
| NFR-P3 | Deterministic path avoids LLM calls | Shipped | `ai_agents/orchestrator/classifier.py`, `ai_agents/tests/test_p6_t4_integration_db.py`, [ADR 0003](../adr/0003-deterministic-core-routed-agents.md) | Thresholds 0.75 (routing) and 0.70 (receipt fields) |
| NFR-P4 | Indexed high-frequency queries | Shipped | `database/migrations/versions/` (0005 indexing strategy incl. the HNSW index, 0010 tsvector, 0012 HNSW tuning), `backend/tests/test_p3_indexes.py`, [P3-T3](../phase_logs/P3-T3_indexing_strategy.md) | |
| NFR-R1 | Graceful degradation on AI failure | Shipped | `ai_agents/tests/test_openrouter_degradation.py`, `ai_agents/tests/test_p6_t5_degradation_db.py`, [P6-T5](../phase_logs/Phase6_P6-T5.md) | Output marked `degraded` / `needs_review`, never an error page |
| NFR-R2 | Transactional state transitions | Shipped | `backend/app/core/concurrency.py`, `backend/tests/unit/test_unit_concurrency_idempotency.py`, `backend/tests/test_c7r_18_patch_draft_race_db.py` | One commit per request; row locks and `version` checks (`If-Match`) |
| NFR-R3 | Idempotent receipt upload and expense creation | Shipped | `backend/app/core/idempotency.py`, `backend/tests/api/test_idempotency.py` | `Idempotency-Key` header on state-changing endpoints |
| NFR-R4 | Append-only audit trail | Shipped (changed) | `backend/app/core/audit.py`, `database/models/audit_log.py` | Append-only by convention; the database does not block updates or deletes |

---

## 5. Success metrics

| ID | Metric | Status | Evidence | Note |
|---|---|---|---|---|
| M-T1 | Time from intent to bookable recommendation | Deferred | | Not instrumented; no user timing study |
| M-T2 | Time to a submittable report | Deferred | | Not instrumented |
| M-T3 | Fewer manual fields per entry | Deferred | | Not measured; per-field provenance would make it measurable |
| M-A1 | Field-level extraction accuracy | Partial | `evals/datasets/m_a1_extraction.jsonl`, `backend/tests/eval/test_eval_db_extraction.py` | 85 receipts: 85/85 routing paths, 510/510 provenance labels with stub OCR and LLM. This validates the plumbing, not vision accuracy |
| M-A2 | Smart-match accuracy | Shipped | `evals/datasets/m_a2_match.jsonl`, `evals/thresholds.py` | F1 1.0 on 65 gold links (floor 0.95) |
| M-A3 | Policy-decision correctness | Shipped | `evals/datasets/m_a3_policy.jsonl`, `evals/harness/policy_decision.py` | 66 cases; confusion matrix fully diagonal; violation recall 1.0 is a hard gate |
| M-R1 | Receipts resolved without manual correction | Partial | `ai_agents/receipt_pipeline/correction.py` | Every corrected field is recorded as `user_edited`, but the rate is not computed or reported |
| M-R2 | Reports assembled and submitted without manual lines | Deferred | | Not measured |
| M-R3 | Auto-approved vs escalated | Shipped | `backend/app/modules/analytics/service.py` | Finance "Automation Rate" KPI = auto-approved ÷ all approvals |
| M-R4 | Deterministic vs LLM route share | Shipped (changed) | `ai_agents/tests/test_p6_t4_integration_db.py`, `ai_agents/tests/test_orchestrator_db.py` | Measured from `ai_decision_logs` in tests (a 12-input characterised set that must be deterministic-first); not shown on a dashboard |

---

## 6. Out of scope (held)

| ID | Item | Status | Note |
|---|---|---|---|
| OOS-01 | Live inventory, booking, ticketing | Out of scope | Mock inventory only |
| OOS-02 | Real payments and disbursement | Out of scope | Reimbursement is a simulated state with a `REIMB-…` reference; no money moves |
| OOS-03 | Invoice / accounts payable | Out of scope | Not built |
| OOS-04 | ERP / HR / accounting integrations | Out of scope | Not built |
| OOS-05 | Off-channel booking capture, events, meeting location | Out of scope | Not built |
| OOS-06 | Multi-agent orchestration product | Out of scope | Four task routes behind one orchestrator |
| OOS-07 | Native mobile apps | Out of scope | Web app only |
| OOS-08 | Multi-tenant SaaS admin, SSO/SAML/SCIM, billing | Out of scope | Data is org-scoped and login is org-aware, but there is no tenant administration, SSO or billing |
| OOS-09 | Disruption rebooking, voice, deep personalisation | Out of scope | Not built |

---

## Built beyond the original PRD

After the MVP (phases 1–8) and the RAG engine (R0–R6), phases 9–12 closed product gaps found in acceptance testing and hardened the public demo. None of this was in the PRD.

| Area | What was built | Phase | Evidence |
|---|---|---|---|
| Multi-document RAG engine | Hybrid dense + sparse retrieval, RRF, cross-encoder rerank, Platt-calibrated abstention, per-sentence support gate. recall@10 0.819 on the deterministic CI path, 0.959 with a live cross-encoder; faithfulness 0.981 at 74% grounded (keyed, offline) | R0–R6 | `ai_agents/rag/`, [RAG docs](../RAG/README.md), [final metrics](../RAG/metrics/FINAL_METRICS_SUMMARY.md) |
| Simulated reimbursement | Finance "Ready to reimburse" queue, Mark reimbursed, `REIMB-<date>-<id>` reference, cycle time measured submitted → reimbursed | 9 | `backend/app/modules/lifecycle/`, `backend/tests/test_pg_lifecycle_db.py`, [log](../phase_logs/phase_9_product_gaps_lifecycle.md) |
| Withdrawal | Owner withdraws a returned report or trip; lines released for re-filing, reviewed receipts kept for audit | 9 | same as above |
| Trip completion | Mark completed on an ended trip; boot-time elapsed-trip sweep with `dry-run` / `off` controls | 9, 10 | `database/seeds/complete_elapsed_trips.py`, `backend/tests/test_kl_elapsed_sweep_db.py`, [runbook](../runbooks/elapsed-trip-sweep.md) |
| Notifications | Bell with unread count, mark read / all read, links to the subject | 9 | `backend/app/modules/notifications/`, `backend/tests/test_pg_notifications_db.py`, [log](../phase_logs/phase_9_product_gaps_shell.md) |
| Global search | Search over trips, reports, entries, receipts, approvals and rules, scoped like the list endpoints | 9 | `backend/app/modules/search/`, `backend/tests/test_pg_search_db.py` |
| Users admin | Add, edit, deactivate / reactivate, governance guards (last finance admin, no self-change) | 9 | `backend/app/modules/identity/admin.py`, `backend/tests/test_pg_users_admin_db.py` |
| Team view | Manager and finance team pages with drill-down | 9 | `backend/app/modules/team/`, `backend/tests/api/test_pg_team_view.py`, [log](../phase_logs/phase_9_product_gaps_team.md) |
| Finance filters and CSV export | Approval filters (exception type, department, owner, dates); CSV export of approvals and audit log | 9 | `backend/tests/api/test_pg_finance_filters.py`, `backend/tests/api/test_pg_csv_export.py` |
| Card feed | Card transactions tab with original and converted amounts | 9 | `backend/app/modules/card/`, `backend/tests/test_pg8_card_feed_api_db.py`, [log](../phase_logs/phase_9_product_gaps_fxcard.md) |
| Currency conversion | Static simulated reference rates for 11 currencies, applied before policy caps and in totals; unsupported currency escalates | 9 | `config/constants/fx_rates.py`, `ai_agents/tools/fx_policy.py`, `ai_agents/tests/test_pg8_fx_conversion.py` |
| More policy clauses | Entry-level rules, ground-transport sub-types, per-head caps, gift disclosure, mileage (MIL-01..03), process rules (deadlines, advance booking, trip changes, card reconciliation); per-rule change history | 9 | `ai_agents/tools/entry_rules.py`, `spend_policy.py`, `process_rules.py`, `backend/tests/test_pg2_spend_mileage_db.py`, [policy log](../phase_logs/phase_9_product_gaps_policy.md), [policy2 log](../phase_logs/phase_9_product_gaps_policy2.md) |
| Traveller grades | Grade ladder; AIR-04 business class for SVP and above; later AIR-07, LOD-05, NR-01; SVP and VP approval routing | 10, 12 | `config/constants/grades.py`, `ai_agents/tools/grade_entitlements.py`, `backend/tests/test_kl_grade_db.py`, `backend/tests/test_fu_grade_entitlements_db.py`, [phase 10](../phase_logs/phase_10_known_limits_grade.md), [phase 12](../phase_logs/phase_12_followups_grades.md) |
| Organisation-aware login | Email unique per organisation; optional organisation code; constant-work password checks | 10 | `backend/app/modules/identity/service.py`, `backend/tests/test_kl_org_scoped_login_db.py` |
| Re-file lineage and ground worst case | Re-filed returned or rejected claims escalate; unconfirmed ground lines checked at worst case | 10 | `backend/tests/test_kl_refile_lineage_db.py`, `backend/tests/test_kl_ground_worst_case_db.py`, [log](../phase_logs/phase_10_known_limits_refile.md) |
| Account recovery | Password reset, invitations, email verification with hashed one-time tokens; `console` (default), `smtp` or `off` email provider | 12 | `backend/app/modules/identity/accounts.py`, `services/email_provider/`, `backend/tests/test_fu_accounts_db.py`, [log](../phase_logs/phase_12_followups_accounts.md) |
| Rate limiting | Per-IP, per-account, AI-spend and upload budgets; login backoff; optional shared Redis store | 11, 12 | `backend/app/core/rate_limit.py`, `backend/tests/test_ps_rate_limit.py`, `backend/tests/test_rl_redis_store.py`, [runbook](../runbooks/rate-limit-redis.md) |
| Upload hardening | Body caps, magic-byte type checks, decompression-bomb limits, metadata stripping, quotas | 11 | `backend/app/modules/receipt/upload_guard.py`, `backend/app/core/body_limit.py`, `backend/tests/test_ps_upload_guard.py`, [log](../phase_logs/phase_11_public_demo_security_uploads.md) |
| PDF receipts | Rasterise-only in a contained child process; the PDF is never stored | 12 | `backend/app/modules/receipt/pdf_raster.py`, `pdf_render_worker.py`, `backend/tests/test_pdf_receipts.py`, [log](../phase_logs/phase_12_followups_pdf.md) |
| Demo mode and reset | Seeded accounts and rules protected (`DEMO_MODE`); dry-run-first reset tool; optional daily schedule | 11, 12 | `backend/app/core/demo.py`, `database/seeds/reset_demo_data.py`, `backend/tests/test_ps_demo_protection_db.py`, `backend/tests/test_fu_demo_autoreset.py`, [runbook](../runbooks/demo-reset.md) |
| Receipt locking | Receipt evidence on a submitted or reviewed report cannot be retyped or reused for a second claim (`409 RECEIPT_LOCKED`) | 8 (UAT round 2) | `backend/tests/test_uat2_receipt_hardening_db.py`, `backend/tests/test_c7_g3_receipts_api.py` |
| Commit-pinned staging deploy | CI deploys only the tested SHA and verifies it on `/health` and `/api/version`; drilled rollback | 8 | `scripts/ci/wait_for_commit.sh`, [ADR 0005](../adr/0005-commit-pinned-deploy-verification.md), [log](../phase_logs/phase_8_p8_t4_cicd_pipeline.md), [rollback runbook](../runbooks/rollback.md) |
| CI hygiene and secret scan | Blocking secret scan over tree and history; env audit | 11 | `scripts/ops/secret_scan.py`, `backend/tests/test_ci_workflow_hygiene.py`, [log](../phase_logs/phase_11_public_demo_security_ops.md) |

Known limits of what shipped are listed in the [README](../../README.md#known-limitations--roadmap) and in each phase log's residual-risk section.
