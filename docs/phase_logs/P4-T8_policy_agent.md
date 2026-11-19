# P4-T8 — Policy Agent

**Sub-phase:** P4-T8  
**Title:** Policy Agent — deterministic rule eval + grounded Q&A + rule-authoring assist  
**Date:** 2026-06-13  
**Author:** Garv Singh Saini  
**Begins:** Phase 4D (first 4D agent)

---

## WHAT WAS BUILT

### `ai_agents/agents/` — new subpackage

Three capabilities built by composing prior primitives (PolicyEvalTool P4-T4, GroundedRetriever P4-T6, LlmProvider P4-T1, reflection/critic P4-T3, decision-log/citation/role-bound contracts P4-T2, Agent/DeterministicHandler protocols P4-T7). No new primitives introduced.

---

#### Capability 1 — Deterministic Rule Evaluation (`policy_eval_handler.py`)

**Route:** `deterministic`. **LLM:** none. **Registered as:** `DeterministicHandler` under `task_type='policy_evaluate'`.

Triggered when `Task.signals['policy_eval']` is truthy. Parses `EvalItem` from `task.payload`, calls `PolicyEvalTool().evaluate(item, context, session)` (which loads only `status='active'` rules from the DB, scoped to `organization_id`), and returns a `PolicyVerdict` with per-rule `RuleVerdict` entries.

**Citations:** one `Citation(kind=clause_ref, ref=source_clause_ref, source="policy_rules")` per verdict that carries a non-None `source_clause_ref`. This is the P4-T2 citation shape — identical to what GroundedRetriever produces.

**Decision log:** one `ai_decision_logs` row with `agent='policy'`, `route='deterministic'`, `model_provider=None`, `confidence=1.0`, `reflection_outcome=None`. The orchestrator writes its own row; every policy task therefore produces **two** rows total.

**LLM-free guarantee:** `policy_eval_handler.py` imports no `LlmProvider` or `get_llm_provider`. The `_RaisingProvider` hermetic test confirms the LLM is structurally unreachable on this path.

---

#### Capability 2 — Grounded Policy Q&A (`policy_agent.py` + `policy_qa.py`)

**Route:** `llm`. **Registered as:** `Agent(AiAgent.policy)` under `task_type='policy_grounded_lookup'`.

`PolicyAgent._handle_qa()` dispatches:

1. `GroundedRetriever().retrieve(question, context, session)` — returns `status=grounded|silent`.
2. **Silent path** (`status=silent`): returns immediately with `output={'answer_type':'silent', 'answer':None}`, `citations=()`, `confidence=None`, `model_provider=None`. **No LLM claim is ever asserted when the policy is silent.**
3. **Grounded path**: feeds retrieved clauses into `reflect(PolicyQaHypothesizer, PolicyQaCritic)`:
   - `PolicyQaHypothesizer.seed()` calls `LlmProvider.complete()` with clause texts + question → `GroundedResult`.
   - `PolicyQaCritic` checks whether the answer follows *only* from the cited clauses (SUPPORTED / CONTRADICTED+correction / UNSUPPORTED+needs_review); on `ProviderError` degrades to `unsupported+needs_review=True`.
   - `reflect()` runs up to 3 rounds. Passing → `asserted=True`, citations attached. Rejected → `AgentResultStatus.needs_review`, no assertion.

**Decision log:** `route='llm'`, `model_provider=<provider id>` on the grounded path; `model_provider=None` on the silent path.

---

#### Capability 3 — Rule-Authoring Assist (`policy_agent.py` + `policy_authoring.py`)

**Route:** `llm`. **Registered as:** same `Agent(AiAgent.policy)` under `task_type='policy_authoring'`. Finance/admin only.

`PolicyAgent._handle_authoring()` dispatches:

1. **RBAC gate:** `check_action(context, AgentAction.POLICY_MANAGE)` — raises `GuardrailBlocked` if `policy.manage` is absent from `context.granted_permission_keys`. Caught (not propagated); returns `AgentResult(needs_review, {denied:True})` and writes a decision-log row. No LLM invoked.
2. **Allowed:** `reflect(RuleAuthoringHypothesizer, RuleAuthoringCritic)`:
   - `RuleAuthoringHypothesizer.seed()` calls the LLM to translate NL intent → candidate JSON `{rule_type, applies_to, definition, source_clause_ref}`.
   - `RuleAuthoringCritic` runs three-stage validation each round:
     - **(a) Enum check:** `rule_type` ∈ `PolicyRuleType`, `applies_to` ∈ `PolicyAppliesTo`.
     - **(b) Evaluate-candidate round-trip:** calls `PolicyEvalTool().evaluate_candidate(rule_type, applies_to, definition, probe_item)` — confirms the definition is evaluable (not a "malformed/skip" verdict with `observed={}`).
     - **(c) LLM faithfulness check:** the LLM confirms the structured rule accurately captures the NL intent.
   - Pass (≥1 round) → `persist_draft_rule()`: `session.add(PolicyRule(status=draft)) + session.flush()`. **Never `session.commit()`. Never `status='active'`.** Audit deferred to backend endpoint P4-T13 (ai_agents must not import backend).
   - Rejected → `AgentResult(needs_review, {draft_created:False})`, nothing persisted.

---

#### Registration wiring (`__init__.py`)

`register_policy(registry, *, provider=None)`:
```python
registry.register_deterministic_handler("policy_evaluate", PolicyEvalHandler())
registry.register_agent(PolicyAgent(provider=provider))
```

---

## AUTHORIZED DEVIATIONS FROM "REUSE-UNCHANGED"

Both approved at the planning review before implementation began.

### 1. `ai_agents/tools/policy_eval.py` — additive `evaluate_candidate()` method

**Change:** Added one public method `evaluate_candidate(rule_type, applies_to, definition, probe_item) -> RuleVerdict`. It constructs a transient in-memory `PolicyRule` (sentinel `policy_id=UUID(int=0)`, `status=draft`) and dispatches to the private `_evaluate_rule()` method already used by `evaluate()`.

**Why needed:** The rule-authoring critic must confirm a candidate definition is *evaluable*, not just schema-valid. The existing `evaluate()` method only loads `status='active'` rules from the DB — authored drafts would never appear there. `evaluate_candidate()` provides a DB-free round-trip for arbitrary definitions.

**Non-breaking guarantee:** The existing public `evaluate()` contract, its signature, and its behaviour are unchanged. The method is purely additive. All prior `test_tool_policy_eval.py` tests still pass.

### 2. `ai_agents/orchestrator/classifier.py` — additive routing changes

**Change (a):** Added constant `_TT_POLICY_EVALUATE = "policy_evaluate"` and a new `policy_eval` signal branch in `_classify_policy()` → `task_type='policy_evaluate'`, `requires_reasoning=False` (deterministic route, Cap-1).

**Change (b):** Flipped `policy_grounded_lookup` from `requires_reasoning=False` to `requires_reasoning=True` (LLM route, Cap-2).

**Why needed:** As-built P4-T7 classifier had only two policy paths: `policy_authoring` (llm) and `policy_grounded_lookup` (deterministic). Cap-2 (LLM Q&A synthesis) had no clean route; Cap-1 (deterministic eval) shared the lookup task_type. The additive three-way dispatch gives each capability clean 1:1 route semantics.

**Non-breaking guarantee:** `policy_authoring` is unchanged. All P4-T7 classifier tests and router tests still pass after the change (three router tests updated to use the new `policy_eval` signal / `policy_evaluate` task_type — they were testing the same deterministic route, just with the old task_type string).

---

## BRANCH WORKFLOW

Built on `p4-t8-policy-agent` as 5 granular commits:

| Commit | Contents |
|--------|----------|
| `cf1708d` | Step 1: classifier routing + `evaluate_candidate` |
| `c82a4b4` | Step 2: core Cap 1-3 files |
| `5edb2de` | Step 2b: router test fixes (3 tests updated for new signal/task_type) |
| `f2f308f` | Step 2c: ruff E501 fixes across `agents/` |
| `e9b53c0` | Step 3-4: hermetic tests (31) + live-DB tests (17) + layering-law extension |

Squash-merged to `main` as one sub-phase commit. Branch preserved.

---

## KEY DECISIONS

**Three capabilities, two handler types.** Cap-1 registered as `DeterministicHandler` (task_type `policy_evaluate`) — guarantees no LLM call even if a provider is passed to the registry. Cap-2 and Cap-3 registered as the same `Agent(AiAgent.policy)` — `handle()` dispatches internally on `decision.task_type`.

**LLM on exactly two paths.** Deterministic eval (Cap-1) is structurally LLM-free. Q&A synthesis (Cap-2, grounded path) and rule authoring (Cap-3, allowed path) invoke the LLM via the abstracted `LlmProvider`. The silent Q&A path and the RBAC-denied authoring path are also LLM-free despite being routed as `route='llm'` by the classifier.

**Draft-only authoring.** `persist_draft_rule()` always sets `status=PolicyRuleStatus.draft`. It calls `session.flush()` and never `session.commit()` (caller owns the transaction). Activation, auditing, and backend wiring are deferred to P4-T13 — the `ai_agents` package must not import `backend`.

**Two decision-log rows per task.** The orchestrator writes one row (`agent='orchestrator'`); each capability writes its own row (`agent='policy'`) with full `route`/`tools_used`/`citations`/`confidence`/`reflection_outcome`/`model_provider` detail. This matches the two-row design established in P4-T7 and verified in the orchestrator end-to-end hermetic test (Gate 10).

**Silent Q&A guardrail.** When `GroundedRetriever` returns `status=silent` (top similarity below the 0.20 threshold), the agent returns `answer_type='silent'`, makes no policy claim, attaches no citations, and does not call the LLM. `model_provider=None` in the log row records this explicitly.

---

## ISSUES FOUND AND FIXED IN QA

| Commit | Issue | Fix |
|--------|-------|-----|
| `5edb2de` | 3 router tests in `test_orchestrator_router.py` were asserting `policy_grounded_lookup` as the deterministic-route task_type — broken by the classifier flip in Step 1 | Updated tests to use `signals={'policy_eval': True}` and handler registered under `'policy_evaluate'` |
| `f2f308f` | 35 ruff errors in the 5 new `agents/` files (E501 line-too-long, unused imports, import ordering, UP035) | Auto-fixed via `ruff --fix`; 24 remaining E501 violations fixed manually |
| `e9b53c0` | `stream()` return type on fake LLM providers was annotated as `Iterator[LlmResponse]` — should be `Iterator[LlmChunk]` | Caught by mypy; corrected in both `test_policy_agent.py` and `test_policy_agent_db.py` before commit |
| `e9b53c0` | Comment line in `test_orchestrator_router.py` introduced in Step 2b was 105 chars (E501) | Shortened in Step 3-4 commit |

---

## VALIDATION EVIDENCE

**Suite:** 233 passed / 107 skipped (90 pre-existing live-DB skips + 17 new policy-agent live-DB skips). Zero failures.

**All 12 Section 4 gates covered:**

| Gate | What | Status |
|------|------|--------|
| 1 | Eval pass/fail fixtures across all 6 rule types; correct `source_clause_ref` citation; M-A3 no false-pass | ✔ |
| 2 | Eval is LLM-free: `_RaisingProvider` never called; `route=deterministic`, `model_provider=None` in log | ✔ |
| 3 | Q&A grounded: answer cites clause; always-contradicted critic → `needs_review` (over-claim rejected) | ✔ |
| 4 | Q&A silent: off-topic → `answer_type='silent'`, `citations=()`, `model_provider=None`; no LLM call | ✔ |
| 5 | Authoring produces evaluable `cabin_class/flight` draft via `evaluate_candidate` round-trip | ✔ |
| 6 | Authored rule always `status='draft'`; never `status='active'`; `session.flush()` only, no commit | ✔ |
| 7 | RBAC: employee/manager blocked (`denied=True`, no LLM); finance/admin allowed | ✔ |
| 8 | Malformed/unfaithful LLM output → rejected; `session.added == []`, nothing persisted | ✔ |
| 9 | One `agent='policy'` log row per path with correct `route`/`model_provider`/`confidence` fields | ✔ |
| 10 | End-to-end via orchestrator: two rows (`orchestrator` + `policy`); both `route='deterministic'` on eval | ✔ |
| 11 | LLM only via abstracted `LlmProvider`; no real API in CI; `_ScriptedProvider` injects responses | ✔ |
| 12 | Layering law: all `ai_agents.agents.*` modules importable in subprocess without any `backend.*` in `sys.modules` | ✔ |

**ruff:** zero errors across all `ai_agents/` (including new files).  
**mypy:** zero errors in `ai_agents/agents/` and both new test files; 6 pre-existing errors in `test_orchestrator_db.py` and `test_rag_indexer.py` are out of scope.

---

## SCOPE CONFIRMATION

- No migration added.
- No model changed.
- No backend wiring (activation/audit deferred to P4-T13).
- No seed modification.
- `evaluate_candidate` is purely additive and non-breaking.
- Classifier change is the approved additive routing flip only; all P4-T7 tests pass.
- Phase 4 is NOT closed — P4-T9 (Travel Agent, ReAct booking copilot) follows.
