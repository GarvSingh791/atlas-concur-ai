# P4-T9 — Travel Agent (FR-T1..T7)

**Sub-phase:** P4-T9
**Date:** 2026-06-14
**Author:** Garv Singh Saini

---

## WHAT WAS BUILT

9 new files under `/ai_agents` — zero existing files modified.

| File | Purpose |
|---|---|
| `ai_agents/agents/travel_agent/__init__.py` | Package exports; `register_travel(registry)` helper |
| `ai_agents/agents/travel_agent/intent.py` | `TripIntent` dataclass (5 required slots); `parse_intent()`; `missing_slot_question()` |
| `ai_agents/agents/travel_agent/ranking.py` | `RankedOption`; `retrieve_and_rank()` (InventoryTool + PolicyEvalTool); `_rank_score()` (4-objective); `_templated_explanation()` |
| `ai_agents/agents/travel_agent/reflection.py` | `TravelShortlistHypothesizer`; `TravelPolicyHonestyCritic` (deterministic policy-honesty check) |
| `ai_agents/agents/travel_agent/narrative.py` | `enrich_with_narratives()` — optional per-option LLM narrative; `ProviderError` → templated fallback + `degraded=True` |
| `ai_agents/agents/travel_agent/persistence.py` | `persist_trip_selection()` — writes `Trip` + `TripSegment` via `database.models`; `session.flush()` only; `append_assistant_message()` |
| `ai_agents/agents/travel_agent/agent.py` | `TravelAgent` — 10-step deterministic ReAct pipeline; implements the Agent protocol (P4-T7) |
| `ai_agents/tests/test_travel_agent.py` | 20 hermetic tests (Gates 1–5, 7–10); no DB, no LLM, no network |
| `ai_agents/tests/test_travel_agent_db.py` | 5 live-DB tests (`@pytest.mark.db`; Gates 6, 7b, 8b); self-skip when Postgres unreachable |

---

## PIPELINE

The 10-step deterministic ReAct pipeline in `TravelAgent.handle()`:

1. **Intent parse** (`intent.py`) — extract `TripIntent` from `task.payload`; identify the 5 required slots: `origin`, `destination`, `departure_date`, `travelers`, `currency` (FR-T1).
2. **Clarify** — if a required slot is missing, return exactly one focused question; `status=ok`, `route=deterministic`, no shortlist, no LLM (FR-T2). Log + return immediately.
3. **Role / identity check** — if `intent.traveler_id` is set and differs from `context.user_id`, deny immediately: `status=needs_review`, logged, no retrieval (NFR-SEC6).
4. **Retrieve** (`ranking.py`) — `InventoryTool.search` for flights (and hotels when in scope); empty result → "no options found", never fabricated (FR-T3).
5. **Evaluate per option** — build `EvalItem` for each candidate, call `PolicyEvalTool.evaluate_many` → `PolicyVerdict` with `in_policy` label, `source_clause_ref` → `clause_ref` citation (FR-T5).
6. **Rank** — deterministic 4-objective score: policy compliance (+100 for in-policy), price (inverse), sustainability (lower impact = higher score), traveler preference (from `ProfileTool`). Out-of-policy options are labeled and shown, never silently top-ranked (FR-T4).
7. **Reflect** — `reflect(TravelShortlistHypothesizer, TravelPolicyHonestyCritic)` from P4-T3. The deterministic critic checks `in_policy == rule_in_policy` for every option; mislabeled options are corrected and demoted → `outcome=revised`. No LLM in the reflection path (FR-A3).
8. **Explain** — templated per-option plain-language summary by default. If `explain_with_llm=True`, calls `LlmProvider.complete` via the injected provider; `ProviderError` → templated fallback + `degraded=True`; `route=llm` only when the model ran without degradation (FR-T6, NFR-R1).
9. **Persist-on-selection** — when `selected_option_ids` present: `check_action(context, AgentAction.TRIP_CREATE)` (raises `GuardrailBlocked` if `TRIP_CREATE_OWN` absent), then `persist_trip_selection()` writes `Trip` + `TripSegment` via `database.models` with `session.flush()` only; `status=draft`; `recommendation_context` populated (FR-T7).
10. **Decision log** — every invocation writes one `ai_decision_logs` row via `write_decision_log()` (agent, task_type, route, tools_used, output_summary, confidence, reflection_outcome, latency_ms) (M-R4).

Additionally, when `conversation_id` is present in the payload, `append_assistant_message()` appends one assistant `ConversationMessage` (tool_calls trace + citations) at `max(sequence) + 1`.

---

## KEY DECISIONS

### 1. Persistence via ai_agents flush-only helper

`persist_trip_selection()` in the agent package writes `database.models.Trip` + `TripSegment` directly using `session.flush()` only — never `session.commit()`. Fields: `status=TripStatus.draft`, `recommendation_context` (selected options + reflection outcome snapshot). Commit, audit trail, and `draft→pending_approval` transition are deferred to the P4-T13 HTTP endpoint.

This is identical to `persist_draft_rule()` in P4-T8 and satisfies the layering law (no `backend/` import), the no-commit rule (agent never owns the transaction), and FR-T7 (`recommendation_context` populated).

### 2. Conversation lifecycle is a caller concern

The agent appends ONE assistant `ConversationMessage` (tool_calls trace + policy citations, at `max(sequence) + 1`) only when `payload["conversation_id"]` is present. The P4-T13 endpoint is responsible for creating the conversation and appending the user turn. The agent is standalone-testable and the message persistence step is simply skipped when no conversation_id is supplied.

### 3. LLM scope = deterministic core + optional narrative

The entire decision path — intent parsing, retrieval, policy evaluation, ranking, and the reflection critic — is deterministic and makes no LLM call. The LLM is invoked only for the per-option "why this option" plain-language narrative (FR-T6), gated by `payload["explain_with_llm"]`. On `ProviderError` the agent falls back to templated explanations, sets `degraded=True`, and returns `status=needs_review`. `route=llm` is recorded only when the model actually ran without degradation; all other paths record `route=deterministic`.

### 4. No registry wiring in this sub-phase

`register_travel(registry, *, provider=None)` exists and registers `TravelAgent` with any `AgentRegistry`. The call is not placed here; composition-root wiring (alongside all other agents) is P4-T13's responsibility.

---

## DEVIATIONS FROM THE TASK SPEC

### Spec-level deviation: persistence via ai_agents helper

The task spec instructed: "persist ONLY through the travel module service (P2-T6)." The reconcile loop proved this infeasible on three independent grounds:

- **Layering law** — `ai_agents` must never import `backend/`; `backend.app.modules.travel.service` is unreachable.
- **Missing field** — `create_trip` hardcodes `recommendation_context=None` and the `TripCreate` schema has no such field; FR-T7 requires it populated.
- **Session ownership** — `create_trip` calls `session.commit()`; agents must only flush (transaction boundary is the HTTP endpoint).

The code won over the task spec — exactly the reconcile loop working as intended. Resolution: flush-only `persist_trip_selection()` in the agent package, mirroring P4-T8's `persist_draft_rule()`.

### Interpretation note: "ReAct loop" in a deterministic system

The design docs describe the Travel Agent as a "ReAct loop / generator→critic," which classically implies an LLM driver for each action. Because intent arrives pre-parsed (from the P4-T7 classifier/router) and NFR-P3 mandates deterministic-first, the implementation is a ReAct-shaped deterministic pipeline with the LLM as an optional presentation layer. This is compliance with NFR-P3, not a gap.

---

## VALIDATION EVIDENCE

| Gate | Description | Result |
|---|---|---|
| 1 | Happy path SFO→JFK — ranked, labeled shortlist with rationales | **PASS** |
| 2 | Clarify — missing slot → exactly one question, no premature options | **PASS** |
| 3 | No-fabrication — unseeded route → empty shortlist, nothing invented | **PASS** |
| 4 | Policy honesty — cheapest OOP option labeled, not top-ranked | **PASS** |
| 5 | Reflection catch — mislabeled option corrected, outcome=revised | **PASS** |
| 6 | Persist Trip + TripSegments with recommendation_context | **SKIP** (live-DB only; `@pytest.mark.db`) |
| 7 | Role-bound — cross-user denied; missing TRIP_CREATE_OWN blocks persist | **PASS** |
| 8 | Exactly one ai_decision_logs row per invocation | **PASS** |
| 9 | Deterministic path — raising provider never fires, route=deterministic | **PASS** |
| 10 | ProviderError → templated fallback + needs_review, shortlist intact | **PASS** |

**Suite:** 20/20 hermetic tests pass; full suite 328 passed / 265 skipped / 0 failed.
**Static analysis:** `ruff check` clean; `ruff format` clean; `mypy` clean (9 source files).
**Drift guard:** `git diff` on tracked files empty — no migration, no model, no backend, no orchestrator change.

---

## STATUS

Phase 4 in progress. P4-T9 complete.

**Open:**
- Gate 6 live-DB run (`pytest -m db ai_agents/tests/test_travel_agent_db.py`) against a seeded Postgres stack.
- P4-T13 endpoint wiring: `register_travel(registry)` + conversation creation + HTTP surface.

**Next:** P4-T10 — Expense Agent.
