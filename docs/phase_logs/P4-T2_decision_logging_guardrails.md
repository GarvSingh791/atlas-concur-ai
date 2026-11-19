# P4-T2 — Decision Logging + Guardrail Contract

**Sub-phase:** P4-T2  
**Title:** Decision Logging + Guardrail Contract  
**Date:** 2026-06-13  
**Status:** COMPLETE & VERIFIED  
**Author:** Garv Singh Saini

---

## Objective

Build the cross-cutting guardrail + audit substrate under `ai_agents/` that
every future agent will rely on. Nothing in Phase 4 consumes these pieces yet;
the gate is satisfied by stub invocations proving both conditions: a stub record
writes a complete `ai_decision_logs` row, and a disallowed action is blocked.

Design principles enforced here:
- **Strict downward-only layering**: `ai_agents/` imports ONLY `config/`,
  `database.models`, `services/`, and stdlib. Zero imports from `backend/`
  (no FastAPI, no `AppError`, no backend RBAC, no backend logging) — proven by
  subprocess isolation test.
- **Injected session + permission set**: `ai_agents` never opens a DB session
  and never resolves a user or permissions from the database; callers inject
  both, keeping the intelligence plane testable without the web layer.
- **Key-based authorization only (NFR-SEC6)**: every permission check is a
  set-membership test on the injected `granted_permission_keys`; role names are
  advisory/observability only, never used to gate access.
- **PII-safe logs (NFR-SEC5)**: all three JSONB summary fields pass through the
  minimizer before any data reaches the session.

---

## What Was Built

### `ai_agents/__init__.py` — package root
Declares the layering contract in the module docstring.  No application exports
at this level; subdirectory packages export their own public surfaces.

### `ai_agents/context.py` — `AgentContext`
Frozen dataclass: `organization_id` (required UUID), `granted_permission_keys`
(`frozenset[str]` — the resolved permission set, injected by caller),
`user_id` (optional UUID), `role` (advisory RoleKey value, never used for
authorization), `correlation_id` (optional, injected — NOT pulled from the
backend ContextVar). Future seam: `backend/modules/ai` (P4-T13) constructs
this after calling `get_user_permissions()` via backend RBAC.

### `ai_agents/exceptions.py` — `GuardrailError` / `GuardrailBlocked`
Exception hierarchy mirroring `services/exceptions.py::ProviderError` shape
(`code` class attribute, `message` instance attribute, optional `code`
keyword override). `GuardrailError` is the base (validation/configuration
problems). `GuardrailBlocked` is raised when a permission check fails.
Neither imports `AppError`; both are safe for logging (no secrets, no PII).

### `ai_agents/decision_log/` — decision-log writer

**`record.py` — `DecisionRecord`**  
Dataclass describing one AI invocation before persistence: `agent`, `task_type`,
`route` (all required, typed as the real DB enum members), plus optional
`model_provider`, `input_summary`, `tools_used`, `output_summary`,
`confidence`, `reflection_outcome`, `latency_ms`.

**`minimizer.py` — `minimize()`**  
Pure recursive PII/secret redactor (no I/O, no exceptions). Two-pass strategy:
1. Key-based: dict keys matching `password|secret|token|api_key|authorization|
   ssn|credit_card` → `"[REDACTED]"`.
2. Value-based: regex masking for Bearer tokens (`\bBearer\s+\S+`), e-mail
   addresses, and credit-card-like 16-digit runs.

Applied to `input_summary`, `output_summary`, and `tools_used` before any
data reaches the session.

**`writer.py` — `write_decision_log()` + `latency_timer()`**  
Mirrors `backend/app/core/audit.py::record_audit` semantics exactly:
- Accepts an injected `Session`; performs `session.add()` + `session.flush()`
  (surfaces DB/enum errors immediately, populates `id`/`created_at` from
  server defaults). **Does not commit** — caller owns the transaction.
- Validates `confidence` to `[0.000, 1.000]` and quantizes to 3 decimal places
  (`Decimal`, `ROUND_DOWN`) before persistence; out-of-range → `GuardrailError`.
- `reflection_outcome` accepted as a pass-through field value; no reflection
  logic (deferred to P4-T3).
- `latency_timer()` — lightweight `contextlib.contextmanager` using
  `time.perf_counter`; yields a 1-element `list[int]` that the caller assigns
  to `DecisionRecord.latency_ms`.

### `ai_agents/guardrails/` — grounding + role-bound check

**`citations.py` — `Citation` / `GroundedResult` / `validate_grounding()`**  
Grounding/citation contract (FR-A4):
- `CitationKind` enum: `clause_ref | source_field | tool_result`.
- `Citation` frozen dataclass: `kind`, `ref` (clause id / field name / tool-result
  id), optional `detail` and `source`. `.to_dict()` → plain JSON dict.
- `GroundedResult` frozen dataclass: `claim`, `value`, `citations`, `confidence`,
  `asserted`. `.to_dict()` → `{"asserted": bool, "confidence": float,
  "citations": [...]}` — the list-of-citation-dicts shape that serializes
  cleanly into `conversation_messages.citations` and `ai_decision_logs` JSONB.
- `validate_grounding(result, *, threshold=0.7)`: returns `asserted=True` only
  when `confidence >= threshold` AND `len(citations) >= 1`; otherwise returns a
  copy with `asserted=False` (the "policy is silent" / needs-review outcome).
  **Never raises. Never promotes an uncited claim to asserted status.**

**`actions.py` — `AgentAction` / `ACTION_PERMISSION_MAP` / `check_action()`**  
Role-bound action check (NFR-SEC6):
- `AgentAction` enum (state-changing operations only): `TRIP_CREATE`,
  `TRIP_UPDATE_STATUS`, `EXPENSE_REPORT_CREATE`, `EXPENSE_REPORT_SUBMIT`,
  `APPROVAL_DECIDE`, `POLICY_MANAGE`.
- `ACTION_PERMISSION_MAP: dict[AgentAction, str]` — built exclusively from
  imported constants (`TRIP_CREATE_OWN`, `TRIP_UPDATE_OWN`, `EXPENSE_CREATE_OWN`,
  `EXPENSE_UPDATE_OWN`, `APPROVAL_DECIDE_REPORTS`, `POLICY_MANAGE`). No
  hardcoded key strings.
- `check_action(context, action)`: allow if `ACTION_PERMISSION_MAP[action] in
  context.granted_permission_keys`, else raise `GuardrailBlocked`. A forged role
  name in the context cannot escalate — the check is purely key-membership based.
  A prompt-injected instruction cannot request an action outside the enumerated
  set.

### `ai_agents/tests/` — test suite (4 new files)

**`test_citation_contract.py`** (hermetic, 14 tests)  
Citation contract: cited + above-threshold → `asserted=True`; zero citations
→ `asserted=False`; below-threshold with citations → `asserted=False`;
`to_dict()` serializes to the expected JSONB shape with all optional fields.

**`test_role_bound_check.py`** (hermetic, 26 tests)  
Role check: principal without the key → `GuardrailBlocked`; with it → allowed;
all 6 `AgentAction` values parametrized; **key-based proof** — swapping the
`role` name while keeping the same key set yields identical outcome; forged
`finance_admin` role without the key is still blocked.
`ACTION_PERMISSION_MAP` completeness: all actions have an entry.

**`test_no_backend_import.py`** (hermetic, 1 test)  
Subprocess isolation: imports all `ai_agents.*` modules in a fresh Python
process and asserts no `backend.*` module entered `sys.modules`.

**`test_decision_log_writer.py`** (`@pytest.mark.db`, 13 live-DB tests + 2
hermetic `latency_timer` tests = 15 total; live-DB self-skips in CI)  
End-to-end write: one `ai_decision_logs` row, all required fields, confidence
to 3 dp, PII/email/Bearer/card redacted, `reflection_outcome` round-trips,
nulls accepted, **no-commit proof** (rollback before commit → row absent in
a separate session), out-of-range confidence → `GuardrailError`, deterministic
route + null model_provider accepted.

---

## Key Decisions

### (a) Writer lives in `ai_agents/`, not `backend/core/`
The dependency map (`ai_agents` may import `database.models`) makes this
natural. The writer is not part of the web layer; placing it in `ai_agents/`
keeps the intelligence plane self-contained and testable in isolation.
The seam to `backend/core/` is preserved: the session is injected by the caller
(eventually `backend/modules/ai`, P4-T13) — same as the audit interface pattern.

### (b) `ACTION_PERMISSION_MAP` built from imported constants, not literals
Every value in the map is an imported symbol from `config/constants/permissions`.
A key rename in that module is a compile-time import error, not a silent runtime
divergence. No role-name literals anywhere in the action-check logic.

### (c) PII minimizer applied to all three JSONB fields
The task spec specifies `input_summary` and `output_summary`. `tools_used` was
added deliberately — tool call inputs and outputs frequently carry PII (search
terms, amounts, identifiers) and are persisted in the same append-only log.
Applying the minimizer to all three is strictly safer with no downside.

### (d) `reflection_outcome` field is wired only; critic loop is P4-T3
`DecisionRecord.reflection_outcome` is a pass-through typed field. `write_decision_log`
accepts and persists it. No reflection loop, no critic agent, no re-routing
logic. That is entirely P4-T3's responsibility.

### (e) Correlation ID is injected, not pulled from the backend ContextVar
`ai_agents` must not import `backend.app.core.correlation`. The correlation ID
arrives in `AgentContext.correlation_id`, populated by the caller from the ASGI
scope or background-task header. This preserves the layering fence.

---

## Files Created

```
ai_agents/__init__.py
ai_agents/context.py
ai_agents/exceptions.py
ai_agents/decision_log/__init__.py
ai_agents/decision_log/record.py
ai_agents/decision_log/minimizer.py
ai_agents/decision_log/writer.py
ai_agents/guardrails/__init__.py
ai_agents/guardrails/citations.py
ai_agents/guardrails/actions.py
ai_agents/tests/test_citation_contract.py
ai_agents/tests/test_role_bound_check.py
ai_agents/tests/test_no_backend_import.py
ai_agents/tests/test_decision_log_writer.py
docs/phase_logs/P4-T2_decision_logging_guardrails.md
```

No existing file was modified. No new DB table, column, or migration.

---

## Validation

Both P4-T2 gate conditions met:

| Condition | Status |
|-----------|--------|
| Stub invocation writes a complete `ai_decision_logs` row | ✔ `test_writes_exactly_one_row` (live-DB) |
| Disallowed action is blocked | ✔ `test_action_blocked_when_key_absent` (hermetic) |

Full suite results:

| Check | Result |
|-------|--------|
| pytest (full suite) | **119 passed, 171 skipped** (live-DB/storage self-skip in CI) |
| ai_agents/ hermetic tests | **44 passed, 13 skipped** (13 live-DB tests self-skip) |
| ruff check | Clean |
| ruff format --check | Clean |
| mypy ai_agents/ | 15 files, no issues |
| Layering fence grep | Zero violations (no `backend/`, `fastapi`, or `AppError` in `ai_agents/` production code) |
| Subprocess isolation test | `ai_agents.*` imports produce zero `backend.*` entries in `sys.modules` |
| `/health` endpoint | 200 OK |
| Alembic autogenerate drift | Empty — no model or migration change |
| No new migration | Confirmed |

---

## QA Findings (found and fixed during implementation)

1. **`ai_agents/decision_log/record.py`**: unused `dataclasses.field` import —
   removed (ruff auto-fix).
2. **`ai_agents/guardrails/actions.py`**: import block ordering — sorted by ruff
   (ai_agents-local imports placed after config/constants imports per isort
   first-party ordering rules).
3. **`ai_agents/tests/test_citation_contract.py`**: unused `pytest` import after
   removing `pytest.raises` usages — removed (ruff auto-fix).
4. **`ai_agents/tests/test_decision_log_writer.py`**: unused `sqlalchemy.text`
   import — removed (ruff auto-fix).

---

## Deviations

None. The writer-in-`ai_agents/` placement is plan-aligned, not a deviation.

Standing note from prior phases: `backend/app/core/logging.py`'s
`_CorrelationFilter` automatically injects correlation IDs into all log records;
`ai_agents` logs through the standard `logging.getLogger(__name__)` root handler
and inherits this behavior at runtime without importing any backend module directly.

---

## Next

**P4-T3** — Reflection/critic loop (wires the `reflection_outcome` field
populated here into an actual critic agent).  
**P4-T4** — Grounding tool layer (the `ToolSpec`/`ToolCall` seam built in P4-T1
meets the action-check infrastructure built here).
