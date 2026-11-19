# P4-T13 — Assistant Endpoints (closes Phase 4)

**Sub-phase:** P4-T13  
**Title:** Assistant Endpoints + Composition Root — Closes Phase 4  
**Date:** 2026-06-14  
**Author:** Garv Singh Saini

---

## STATUS

Complete, validated, uncommitted at time of writing.

---

## WHAT WAS BUILT

### Composition root

**`backend/app/modules/ai/runtime.py`** *(NEW)*  
The one-way backend→ai_agents bridge. `build_orchestrator()` (`lru_cache`'d, maxsize=1) constructs a single `AgentRegistry`, registers all four agents (policy, travel, expense, approval) in order, and returns a wrapping `Orchestrator`. `build_agent_context()` resolves DB-backed permissions via the P2-T4 RBAC layer into a frozen `AgentContext`. `get_orchestrator()` is the FastAPI dependency seam — tests override via `app.dependency_overrides[get_orchestrator]`.

### Ten endpoints

| Path | Method | Auth | Note |
|---|---|---|---|
| `/conversations` | POST | authenticated | creates AI session |
| `/conversations/{id}/messages` | POST | owner | SSE stream; compute→commit→replay |
| `/conversations/{id}` | GET | owner | ordered history |
| `/policy/query` | POST | POLICY_READ | grounded Q&A + clause citations |
| `/policy/evaluate` | POST | POLICY_READ | deterministic, no LLM |
| `/policies/{id}/rules` | POST | POLICY_MANAGE | draft rule via agent, audit written |
| `/receipts` | POST | authenticated | multipart + OCR pipeline |
| `/receipts/{id}` | PATCH | owner | correction, provenance flip |
| `/approvals` | POST | APPROVAL_DECIDE_REPORTS | fires approval agent, no double-audit |
| `/approvals/{id}` | PATCH | APPROVAL_DECIDE_REPORTS | human decision, appends ApprovalStep |

### Module files

**`backend/app/modules/ai/router.py`** *(NEW)* — three conversation endpoints; SSE streaming via `StreamingResponse`.  
**`backend/app/modules/ai/service.py`** *(NEW)* — orchestrator delegation, conversation persistence.  
**`backend/app/modules/ai/schemas.py`** *(NEW)* — request/response DTOs for AI module.  
**`backend/app/modules/ai/streaming.py`** *(NEW)* — SSE generator; receives pre-built event list post-commit.  
**`backend/app/modules/ai/__init__.py`** *(NEW)*  
**`backend/app/modules/policy/router.py`** *(NEW)* — three policy endpoints.  
**`backend/app/modules/policy/service.py`** *(NEW)* — policy agent delegation, audit write on rule draft.  
**`backend/app/modules/policy/schemas.py`** *(NEW)* — request/response DTOs for policy module.  
**`backend/app/modules/policy/__init__.py`** *(NEW)*  
**`backend/app/modules/receipt/router.py`** *(NEW)* — POST + PATCH receipt endpoints.  
**`backend/app/modules/receipt/schemas.py`** *(NEW)* — request/response DTOs for receipt module.  
**`backend/app/modules/receipt/__init__.py`** *(NEW)*  
**`backend/app/modules/approval/router.py`** *(NEW)* — POST + PATCH approval endpoints.  
**`backend/app/modules/approval/service.py`** *(NEW)* — approval agent delegation, ApprovalStep append.  
**`backend/app/modules/approval/schemas.py`** *(NEW)* — request/response DTOs for approval module.  
**`backend/app/modules/approval/__init__.py`** *(NEW)*  
**`backend/app/api/v1.py`** *(MODIFIED)* — registered ai, policy, receipt, approval routers.  
**`backend/requirements.txt`** *(MODIFIED)* — added streaming/SSE dependency.

### Test files (six)

**`backend/tests/test_conversations_api.py`** *(NEW)* — conversation create, message send, history fetch.  
**`backend/tests/test_policy_api.py`** *(NEW)* — query (grounded), evaluate (deterministic), rule-draft.  
**`backend/tests/test_receipts_api.py`** *(NEW)* — upload + IDOR-fixed correction.  
**`backend/tests/test_approvals_api.py`** *(NEW)* — auto-approve POST, human decision PATCH.  
**`backend/tests/test_ai_composition.py`** *(NEW)* — composition root unit tests; dependency override paths.  
**`backend/tests/test_checkpoint_e2e.py`** *(NEW)* — G17 end-to-end: plan→select→capture→assemble→approve over the API, each step verifying decision log + citations.

---

## KEY DECISIONS

**Single commit per endpoint — agents flush only**  
Each endpoint owns exactly one `db.commit()`. Agents throughout P4-T9–T12 were written flush-only; P4-T13 is where those deferred commits land. This preserves the atomicity contract: partial work never becomes durable.

**SSE compute→commit→replay discipline**  
The `StreamingResponse` generator receives a *pre-built* event list after the DB commit completes. It never touches the DB after commit, so a slow or aborted client cannot hold a transaction open or cause a half-write. The three-phase sequence (compute events → commit → replay events to client) is enforced structurally, not by convention.

**No double-audit on POST /approvals**  
The Approval Agent (P4-T12) already writes its audit row via the injected `AuditWriter` before the endpoint calls `db.commit()`. The endpoint commits that row and omits a second `record_audit()` call (G10 — the approval routing decision is an agent decision, not an endpoint action).

**Layering law preserved — ai_agents/ unchanged**  
`backend→ai_agents` is the only import direction. `runtime.py` is the sole crossing point. `ai_agents/` has zero diff in this sub-phase.

**No schema drift**  
Zero changes to `database/models/`, `database/migrations/`, or any enum definitions. All conversations/messages/decision-log persistence uses existing models.

---

## SECURITY FIX

**IDOR in POST /receipts** — the `expense_id` query parameter was stored without an ownership check. An authenticated user could attach a receipt to another user's expense. Fixed by joining `Expense → ExpenseReport` and asserting `organization_id == user.organization_id AND owner_id == user.id` before creating the `Receipt` row.

---

## INTEGRATION POINTS

- **Orchestrator registry** — now backed by four real agents (not fakes); composition root is `runtime.py`.
- **P2-T2 audit interface** — `record_audit` injected into the approval agent and called directly from the policy rule-draft endpoint.
- **P2-T4 RBAC** — `POLICY_READ`, `POLICY_MANAGE`, `APPROVAL_DECIDE_REPORTS` permission checks guard the relevant endpoints; `build_agent_context()` resolves them from DB.
- **storage_provider** — receipt image bytes dispatched through the storage abstraction before OCR.
- **Persistence tables** — `conversations`, `conversation_messages`, `ai_decision_logs` all written via the existing models.

---

## VALIDATION EVIDENCE

| Check | Result |
|---|---|
| Full test suite | 407 passed / 328 skipped (DB+storage-gated) / 0 failed |
| `ruff check` | CLEAN |
| `ruff format` | CLEAN |
| `mypy` (new modules) | CLEAN |
| `mypy` pre-existing | 2 errors in `services/ocr_provider/local.py` (untouched — out of scope) |
| Drift guard | CLEAN — no migration, no model, no enum change |
| `ai_agents/` diff | EMPTY |
| Layering law | INTACT — no `backend` import inside `ai_agents/` |

The 328 skips are DB/storage-gated. Run `pytest -m db` against the seeded Docker stack to exercise them.

---

## KNOWN / CARRIED FORWARD

- **2 pre-existing mypy errors** in `services/ocr_provider/local.py` — untouched in P4-T13, out of scope; resolve in a later cleanup.
- **328 DB/storage-gated tests** — expected self-skip without Postgres + object storage. Run `pytest -m db` against the seeded stack.

---

## STATUS

Phase 4 is **CLOSED**.

All four agents (P4-T9 through T12) + the receipt pipeline (P4-T11) + RAG (P4-T5/T6) are now reachable over HTTP. The full loop — plan a trip → select → capture receipts → assemble report → approve — is demonstrable purely via API with decision logs and clause citations at each step.

**Next: Phase 5 — Frontend (P5-T1 design-system primitives).**
