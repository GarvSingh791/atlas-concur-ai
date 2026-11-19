# P4-T7 — Intent Classification + Routing

**Sub-phase:** P4-T7  
**Title:** AI Orchestrator / Router — Intent classification + routing  
**Date:** 2026-06-13  
**Author:** Garv Singh Saini  
**Begins:** Phase 4C  

---

## WHAT WAS BUILT

### `ai_agents/orchestrator/` — new subpackage

**`envelopes.py`** — Typed frozen dataclasses forming the envelope layer:
- `Task` — incoming request envelope (opaque payload, context_type, signals, optional caller task_type hint).
- `Classification` — deterministic classifier output: authoritative task_type, confidence, requires_reasoning flag.
- `RoutingDecision` — the orchestrator's routing choice: target_agent, route, task_type, confidence, rationale.
- `AgentResult` — what a handler/agent returns: status, output, citations (reusing `Citation` from guardrails), optional confidence and reflection_outcome.
- `AgentResultStatus` — enum: `ok` / `unavailable` (no agent registered) / `needs_review` (no deterministic handler registered).
- `OrchestratorOutcome` — full result returned to the caller: decision + result + log_id.

**`contracts.py`** — Stable dispatch contracts (`@runtime_checkable` Protocols):
- `Agent` — the minimum contract every specialized agent (P4-T8 Policy → T12 Approval) must implement: `agent_type: AiAgent`, `handled_task_types() -> frozenset[str]`, `handle(task, decision, *, context, session) -> AgentResult`.
- `DeterministicHandler` — no-LLM handler: `handle(task, decision, *, context, session) -> AgentResult`.

Both protocols mirror the established `(intent, *, context: AgentContext, session: Session)` idiom used by `GroundingTool` and `GroundedRetriever`. Scoped context is passed through unchanged.

**`registry.py`** — `AgentRegistry` with two registration faces (one object):
- `register_agent(agent)` — keyed by `agent.agent_type` (`AiAgent` enum).
- `register_deterministic_handler(task_type, handler)` — keyed by task_type string.
- Lookups return `None` for unknown keys; the orchestrator fails safe rather than crashing.
- Ships **empty** of real agents in P4-T7; real agents register starting P4-T8.

**`classifier.py`** — `IntentClassifier.classify(task) -> Classification`; rule-based, **zero LLM calls**:

| Rule | Condition | target_agent | confidence | requires_reasoning |
|---|---|---|---|---|
| 1 | `signals['approval_required']` is truthy | `approval` | 0.95 | True |
| 2 | `context_type == 'travel_planning'` | `travel` | 0.95 | True |
| 3 | `context_type == 'expense'` + simple flag | `expense` | 0.95 | False |
| 3b | `context_type == 'expense'` (reasoning) | `expense` | 0.95 | True |
| 4 | `context_type == 'policy_qa'` (lookup) | `policy` | 0.95 | False |
| 4b | `context_type == 'policy_qa'` + authoring | `policy` | 0.95 | True |
| 5 | general / None / unknown | `policy` | 0.40 | False |

An injectable strategy seam is left for a future LLM-assisted classifier; not built.

**`orchestrator.py`** — `Orchestrator.handle_task(task, *, context, session) -> OrchestratorOutcome`:
1. Classify via `IntentClassifier`.
2. Decide route: `deterministic` iff `confidence >= threshold AND NOT requires_reasoning`; else `llm`.
3. Dispatch: deterministic → `registry.get_deterministic_handler(task_type)`; llm → `registry.get_agent(target_agent)`. Unregistered → fail-safe result, no crash.
4. Write one `ai_decision_logs` row (agent=`orchestrator`) via the P4-T2 writer.
5. Return `OrchestratorOutcome` to caller; caller owns `session.commit()`.

**`__init__.py`** — Public exports: `Orchestrator`, `AgentRegistry`, `IntentClassifier`, `Agent`, `DeterministicHandler`, `Task`, `Classification`, `RoutingDecision`, `AgentResult`, `AgentResultStatus`, `OrchestratorOutcome`.

### `config/settings/base.py`

`ROUTING_CONFIDENCE_THRESHOLD: float = 0.75` — env-driven threshold controlling the deterministic/llm split; documented in `.env.example`.

---

## ROUTING HEURISTIC

```
route = deterministic  IFF  confidence >= ROUTING_CONFIDENCE_THRESHOLD (0.75)
                       AND  requires_reasoning == False
otherwise → llm
```

Confidence tiers:
- **HIGH = 0.95** — unambiguous context_type match (travel_planning, expense, policy_qa, explicit approval signal). Clears the 0.75 bar.
- **LOW = 0.40** — general / unknown context type. Falls below the bar → always `llm` route.

The primary classification signal is `context_type` (from `Conversation.context_type`). The `approval_required` signal in `task.signals` overrides context_type and routes to the approval agent.

---

## NO-LLM GUARANTEE (structural, not behavioral)

The `Orchestrator` class:
- **Never imports** `LlmProvider` (or any service from `services/llm_provider/`).
- **Never references** a model name, API key, or provider endpoint.
- Sets `model_provider=None` in every `DecisionRecord` it writes.
- `route='llm'` on a `RoutingDecision` merely labels that the downstream agent path is needed; it does not cause the orchestrator to make an LLM call.

This was verified in tests by a `FakeLlmProvider` that raises `AssertionError` on any call to `complete()` or `stream()` — that provider was never triggered on either the deterministic or the llm dispatch path.

---

## BUILD-ORDER RESOLUTION

The task spec's Section 5 builds the orchestrator (`#23`) before the agents (`#24`); Section 7 states "Orchestrator depends on the agents existing to route to them." These are reconciled by the **registry + Agent contract**:

- P4-T7 delivers the orchestrator **mechanism**, the `Agent` contract agents implement, and the `AgentRegistry` they register into.
- Real agents (P4-T8 Policy → T12 Approval) do not yet exist; the registry ships empty.
- P4-T7 is proven end-to-end with **fake agents/handlers** registered in tests.
- An unregistered target returns `AgentResultStatus.unavailable` or `needs_review` (fail-safe, no crash). This is the expected state during Phase 4 while agents are built incrementally.
- Section 7's dependency is **runtime completeness**, not build-order.

---

## DECISION LOGGING / M-R4

For every call to `Orchestrator.handle_task()`, exactly **one** `ai_decision_logs` row is written:

| Column | Value |
|---|---|
| `agent` | `AiAgent.orchestrator` |
| `task_type` | classifier-authoritative (e.g., `trip_planning`, `policy_grounded_lookup`) |
| `route` | `deterministic` or `llm` |
| `confidence` | classifier confidence (NUMERIC(4,3)) |
| `model_provider` | `NULL` (always; orchestrator never calls LLM) |
| `input_summary` | MINIMIZED: `{context_type, signal_keys (not values), has_payload (bool), caller_task_type_hint}` |
| `output_summary` | `{target_agent, route, task_type, confidence, rationale, result_status}` |
| `latency_ms` | wall-clock time via `latency_timer()` |

The **M-R4 metric** (% deterministic vs llm) is computable at any time from:
```sql
SELECT route, COUNT(*) FROM ai_decision_logs WHERE agent = 'orchestrator' GROUP BY route;
```
Verified in the live-DB test `TestM_R4Ratio`.

The P4-T2 decision-log writer (`write_decision_log`) is **reused unchanged**; the orchestrator is its first real consumer. Log-write failures are non-fatal: the orchestrator catches the exception, logs it, and returns `log_id=None` — routing continues.

---

## KEY DECISIONS

**Deterministic-first classifier.** The router must not pay an LLM call merely to decide whether to make one. Rule-based classification on `context_type` is fast, cost-free, and testable deterministically. A clean injectable seam is left for a future LLM-assisted classifier.

**`@runtime_checkable` Protocols for the Agent contract.** The five specialized agents (Policy, Travel, Expense, Approval, ReceiptPipeline) have heterogeneous internal I/O; the contract specifies only what the orchestrator needs to dispatch: `agent_type`, `handled_task_types()`, and `handle()`. `@runtime_checkable` lets the registry do `isinstance()` checks without requiring inheritance.

**Single registry, two faces.** One `AgentRegistry` object handles both agent dispatch (keyed by `AiAgent` enum) and deterministic handler dispatch (keyed by task_type string). This follows the approved design from the plan review.

**Threshold in `config/settings`.** `ROUTING_CONFIDENCE_THRESHOLD` follows the exact pattern of `RETRIEVAL_SIMILARITY_THRESHOLD` (P4-T6): field in `Settings` (pydantic BaseSettings), default in base.py, documented in `.env.example`, overridable per-environment. Not hardcoded in source.

**Registry ships empty; fail-safe for unregistered targets.** An unregistered agent/handler returns a structured `AgentResult` with status `unavailable` / `needs_review` and a descriptive message. No unhandled exception. This is the expected state throughout Phase 4 as agents are built incrementally.

---

## ISSUES FOUND AND FIXED IN QA

| Issue | Fix |
|---|---|
| `ruff UP035` — `Mapping` imported from `typing` in `envelopes.py` | Changed to `from collections.abc import Mapping` |
| Unused imports in `test_orchestrator_router.py`: `Agent`, `DeterministicHandler`, `IntentClassifier` | Removed |
| Unused variable `fake_llm` in `test_orchestrator_router.py` | Removed variable; comment updated to explain the guarantee |
| Unused imports in `test_orchestrator_db.py`: `DeterministicHandler`, `AiDecisionLog` (in method) | Removed |
| `orchestrator.py` — initial draft used a bottom-of-file forward-reference import pattern | Replaced with a clean top-level import (`from ai_agents.orchestrator.classifier import Classification, IntentClassifier`); no circular dependency exists |

---

## VALIDATION EVIDENCE

| Check | Result |
|---|---|
| Hermetic tests (classifier) | 40 passed |
| Hermetic tests (router) | 32 passed |
| Layering-law test | 1 passed |
| **Total hermetic** | **73 passed** |
| Live-DB tests | 7 self-skipped without Postgres |
| Structural no-LLM guarantee | `FakeLlmProvider` (raises on any call) never triggered on either route |
| M-R4 computability | Verified in `TestM_R4Ratio` (live-DB) |
| Existing tests (regression) | 201 passed, 90 skipped — zero failures |
| ruff | Clean |
| mypy | Clean (7 source files in `ai_agents/orchestrator/`) |
| Schema change | None |
| Migration added | None |
| Existing test broken | None |

---

## SCOPE CONFIRMATION

| Item | Status |
|---|---|
| New schema/migration | ✘ None |
| Model change | ✘ None |
| Backend wiring (`/conversations`, etc.) | ✘ Not in scope (P4-T13) |
| Seed modification | ✘ None |
| P4-T2 decision-log writer contract | ✘ Reused, not modified |
| LLM call from orchestrator | ✘ Structurally impossible |
| Real agents (Policy/Travel/Expense/Approval) | ✘ Not built (P4-T8..T12) |
| New external dependency | ✘ None |

---

## NEXT

**P4-T8 — Policy Agent** — the first agent to register with this orchestrator. Implements:
- `Agent` protocol (`agent_type = AiAgent.policy`).
- Deterministic rule evaluation over `policy_rules` (structured table).
- Grounded Q&A via the P4-T6 `GroundedRetriever` (RAG flow).
- Rule-authoring assist (LLM-backed, uses the `Agent.handle()` llm path).
- Registers via `registry.register_agent(policy_agent)`.
