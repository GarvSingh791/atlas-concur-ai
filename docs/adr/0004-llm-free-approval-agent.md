# 0004 — The approval agent uses no LLM

- **Status:** Accepted
- **Date:** 2026-06-14 (phase P4-T12); strengthened later with fresh policy re-evaluation, segregation-of-duties checks and grade routing
- **Decider:** Garv Singh Saini

## Context

Approving an expense report releases money. The original design described an approval agent that auto-approves "low-risk, in-policy" items and escalates the rest. The question was whether a language model should take part in that decision: summarising risk, weighing exceptions, or choosing the route.

Two facts settled it. First, approval routing must be predictable and explainable to an auditor in terms of rules, not model output. Second, report content (vendor names, descriptions, justifications) is written by the claimant, so any LLM that reads it can be steered by it.

## Decision

The approval agent (`ai_agents/agents/approval_agent/`) is **100% deterministic: no LLM call and no reflection loop**. It:

1. Loads the subject with organisation scoping.
2. Checks the caller's permission (`check_action`, NFR-SEC6).
3. **Re-runs the policy engine** over the subject's current lines (`evaluate.py::assess_risk`), merged with stored pre-submit flags (a stored blocking flag can only add risk), and adds segregation-of-duties flags: a re-filed returned or rejected claim (`refiled_returned_claim`, `refiled_rejected_claim`).
4. Applies a binary gate (`routing.py::decide_route`). It auto-approves only if **all** hold:
   - an explicit compliant verdict from the fresh evaluation;
   - the subject is in policy;
   - zero blocking flags;
   - no flag requires a named approver;
   - the total is known and at most `LOW_RISK_AUTO_APPROVE_MAX_USD = 500.00`.

   Anything else escalates.
5. Persists the approval with a recorded rationale, writes `audit_log` (`approval.auto_approved` or `approval.escalated`) and one `ai_decision_logs` row with `route = deterministic` and no model provider.

Hard rules: it **never auto-rejects** (rejection is always a human step with a reason), never approves with a flag, never approves the submitter's own item, and if it fails, the submit still opens a pending, not-evaluated approval for a human (`open_approval_on_submission`). A human can still decide on an auto-approved item.

## Alternatives considered

| Option | Why not |
|---|---|
| LLM risk scoring or an LLM "approve / escalate" decision | Not repeatable, hard to audit, and open to prompt injection from claimant-written text. A model must never influence a routing outcome for money. |
| LLM-written rationale on top of the deterministic decision | Adds cost and a second source of truth. The rationale is the structured `risk_summary` and the flags with their clause references, which the approver UI shows directly. |
| Generator → critic reflection like the other agents | A binary threshold check has nothing to reflect on; the invariant code is the critic. |
| Auto-reject clear violations | Would remove the human from a decision that affects someone's reimbursement. Escalation with the clause is enough. |

## Consequences

**Positive**

- Every approval outcome can be reproduced from the data and the rules; tests assert the exact gate, including the boundary at 500 USD.
- Prompt injection in report content cannot change an approval route.
- The approval path has no provider dependency, so it works the same with `local` providers in CI and on staging.

**Negative / trade-offs**

- The 500 USD threshold is a single constant, in USD only, with no per-organisation or per-grade tuning.
- Policy coverage limits automation: anything the engine cannot verify (an unsupported currency, an unknown grade) escalates, which is safe but sends more items to humans.
- There is one deciding approver per item; grade routing chooses which approver, but there is no multi-step approval chain.

## Evidence

- Gate and constant: [`ai_agents/agents/approval_agent/routing.py`](../../ai_agents/agents/approval_agent/routing.py); agent pipeline: [`agent.py`](../../ai_agents/agents/approval_agent/agent.py); risk assessment: [`evaluate.py`](../../ai_agents/agents/approval_agent/evaluate.py).
- Tests: `ai_agents/tests/test_approval_agent.py` (`test_threshold_constant_is_500`, `test_at_threshold_auto_approves`, `test_above_threshold_escalates`, …), `ai_agents/tests/test_approval_agent_db.py`, `backend/tests/unit/test_unit_approval_determinism.py` (`test_approval_service_has_no_llm_provider`, `test_approval_risk_has_no_llm_provider`).
- Human decision path and segregation of duties: `backend/app/modules/approval/service.py::decide_approval`; grade routing: `backend/app/modules/approval/grade_routing.py`.
- Phase log: [`P4-T12_approval_agent.md`](../phase_logs/P4-T12_approval_agent.md).
