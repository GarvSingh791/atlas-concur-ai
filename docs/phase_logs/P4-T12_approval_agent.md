# P4-T12 — Approval Agent

**Sub-phase:** P4-T12  
**Title:** Approval Agent — Deterministic Route, Auto-Approve, Escalate  
**Date:** 2026-06-14  
**Author:** Garv Singh Saini

---

## WHAT WAS BUILT

Seven files: six new, one modified.

### New files

**`ai_agents/agents/approval_agent/evaluate.py`**  
Subject loading via polymorphic `subject_type` + `subject_id` (supports `trip` and `expense_report`). Org-scope check (NFR-SEC6) — rejects cross-organisation access before any evaluation. Policy Evaluation tool call → `verdict`, `reasons`, `clause_refs`. Builds `risk_summary` JSONB: `{ policy_verdict, reasons, clause_refs, amount, pre_submit_flags, policy_notes }`.

**`ai_agents/agents/approval_agent/routing.py`**  
Named constant `LOW_RISK_AUTO_APPROVE_MAX_USD = 500.00` (approved at the planning review before implementation). Binary gate: fully in-policy **AND** `total_amount ≤ 500 USD` **AND** zero policy flags → auto-approve. Any flag **OR** over threshold → escalate. No third path exists.

**`ai_agents/agents/approval_agent/persistence.py`**  
Idempotency check: existing approval row for `subject_id` → returns `needs_review`, creates no duplicate. Flush-only writes for `approvals` + `approval_steps` rows (parent before child to satisfy FK ordering). Injected `AuditWriter` port (interface, not a direct `audit_log` write) — the audit interface session contract is never assumed.

**`ai_agents/agents/approval_agent/agent.py`**  
`ApprovalAgent` pipeline: load subject → policy/risk eval → route → auto-approve or escalate → audit via `AuditWriter` → one `write_decision_log` row. 100% deterministic; no LLM on any path.

**`ai_agents/agents/approval_agent/__init__.py`**  
`register_approval(registry, *, audit_writer=None)` — composition-root wiring deferred to P4-T13.

**`ai_agents/tests/test_approval_agent.py`**  
29 hermetic unit tests — all 13 plan break-tests covered without a DB connection. Mock session, mock policy tool, mock audit writer. Passes without Postgres.

**`ai_agents/tests/test_approval_agent_db.py`**  
13 DB break-tests (BT-1 through BT-13) decorated `@pytest.mark.db`. Self-skip without Postgres. Run with `pytest -m db ai_agents/tests/test_approval_agent_db.py` against a seeded stack.

### Modified file

**`ai_agents/tests/test_no_backend_import.py`**  
One-line extension to include `approval_agent` in the hermetic layering guard. The only permitted modification to an existing file in this sub-phase.

---

## PIPELINE

Five-step fully deterministic pipeline:

1. **Load + org-scope subject** — resolve `subject_type` + `subject_id`; enforce org membership (NFR-SEC6).
2. **Policy Evaluation tool** — call into the policy agent tool layer; receive `verdict`, `reasons`, `clause_refs`.
3. **risk_summary builder** — assemble JSONB with policy verdict, reasons, clause_refs, amount, pre_submit_flags, and policy_notes.
4. **Binary low-risk gate** — all three conditions must hold simultaneously: fully in-policy **AND** `total_amount ≤ LOW_RISK_AUTO_APPROVE_MAX_USD` **AND** zero policy flags. Any single failure routes to escalate.
5. **Route**:
   - **Auto-approve path** → `current_status='auto_approved'`, `is_auto_approved=TRUE`, `approval_steps.decision='auto_approve'`, `approver_id=NULL`, `reason` always populated.
   - **Escalate path** → `current_status='pending'`, `is_auto_approved=FALSE`, `approver_id=submitter.manager_id`, `risk_summary` carries specific flags + `clause_refs`.
   - → Audit via injected `AuditWriter` → one `ai_decision_logs` row (`agent='approval'`, `route='deterministic'`).

---

## KEY DECISIONS

**`LOW_RISK_AUTO_APPROVE_MAX_USD = 500.00`**  
Named constant in `routing.py`; proposed at the planning review and explicitly approved before implementation. Tunable without touching gate logic. Gate requires all three conditions simultaneously — a single failure (one flag, one dollar over threshold) escalates.

**100% deterministic — no LLM on any path**  
Unlike P4-T9/T10 which carry an optional explanation layer, the Approval Agent uses no language model. The rationale is always the structured `risk_summary`; no generated narrative. This is the correct implementation of NFR-P3 for an approval decision: a model must never influence a workflow routing outcome.

**Audit written inside the agent, not deferred to P4-T13**  
Deliberate and documented departure from P4-T9/T10/T11. The approval routing decision is itself an auditable workflow event that must be immediately durable. Deferring it to the endpoint would create a compliance gap: an auto-approve row exists in the DB without an audit record if the endpoint call fails between flush and commit. The `AuditWriter` port is injected so the session contract is explicit and tests can mock it without a real DB connection.

**`AuditWriter` as injected port**  
The P2-T2 audit interface opens its own connection in the live implementation. Wrapping it as an injected dependency prevents the agent from assuming the session contract and allows hermetic unit tests without a DB connection.

**`reflect()` not wired**  
The Approval Agent's safety net is the hard invariant code (`NEVER auto-reject`, `NEVER approve with flags`, `NEVER approve without rationale`), enforced as explicit assertions in the gate and persistence layer. A generator-critic loop adds no value when the decision is a binary threshold check — the invariant code is the critic. Documented as a named ruling, not an oversight.

**Idempotency guard in `persistence.py`**  
A second run on the same `subject_id` returns `needs_review` and creates no duplicate `approvals` row. The optimistic-lock version column is read before any write.

---

## HARD INVARIANTS

Enforced in code; tested explicitly:

- **NEVER auto-reject** — `current_status='rejected'` is never set by the agent. Only a human approver (P4-T13) may reject.
- **NEVER approve with any policy flag** — regardless of amount.
- **NEVER approve without `approval_steps.reason` populated** — rationale is mandatory on every path.
- **NEVER self-approve** — the agent acts as the system; submitter role does not bypass the gate.
- **NEVER write `audit_log` directly** — only via the injected `AuditWriter`.
- **NEVER call `session.commit()`** — flush-only; commit deferred to P4-T13 for human-action paths.

---

## VALIDATION EVIDENCE

| Check | Result |
|---|---|
| 29/29 hermetic unit tests | PASSED |
| All 13 plan break-tests covered without DB | PASSED |
| 13 DB break-tests (`@pytest.mark.db`) | SELF-SKIP without Postgres (expected) |
| Full suite 374/374 | PASSED / 0 failures / 0 regressions |
| `ruff check` | CLEAN |
| `ruff format` | CLEAN |
| `mypy` | CLEAN (2 pre-existing P4-T1 errors in `services/ocr_provider/local.py` out of scope) |
| Drift guard | EMPTY — no migration, no model, no backend, no orchestrator change |

Break-tests covered (hermetically, without DB):

- Auto-approve happy path (fully in-policy, ≤500 USD, zero flags)
- Escalate on any policy flag
- Escalate when amount exceeds threshold
- Trip subject polymorphism (`subject_type='trip'`)
- NEVER auto-reject invariant
- NEVER self-approve invariant
- NEVER approve with flags invariant
- `approval_steps.reason` always populated
- Audit written via `AuditWriter` per run
- Idempotency: second run → `needs_review`, no duplicate row
- One `ai_decision_logs` row per run; `agent='approval'`, `route='deterministic'`
- Flush-only: transaction rolls back cleanly; `AuditWriter` mock confirms no independent commit
- Unknown `subject_type` → `needs_review`; no crash; no partial write

---

## STATUS

Phase 4 in progress. **P4-T12 complete.**

This is the **FINAL AGENT** — all four specialized agents are now complete:
- P4-T9: Travel Agent
- P4-T10: Expense Agent
- P4-T11: Receipt Analysis Pipeline
- P4-T12: Approval Agent ← this sub-phase

**Open:**
- Live-DB break-tests against seeded stack: `pytest -m db ai_agents/tests/test_approval_agent_db.py`
- P4-T13 — Conversational endpoints: composition-root wiring for all four agents + receipt pipeline, streaming assistant, HTTP triggers for upload/correction/approval actions, commit + audit for all deferred paths.

**Next: P4-T13 — Assistant endpoints.**
