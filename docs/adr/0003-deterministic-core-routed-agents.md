# 0003 — Deterministic core, routed agents

- **Status:** Accepted
- **Date:** 2026-06-13 (orchestrator and classifier, phase P4-T7); applied to every agent built after it
- **Decider:** Garv Singh Saini

## Context

Travel and expense work is regulated: a policy decision or an approval has to be explainable, repeatable and auditable. LLMs help where language or judgement is needed (reading a messy receipt, understanding "a few days in New York next month", answering a policy question in plain English), but they are slow, cost money per call, can be wrong with confidence, and can be steered by text a user controls.

Most of the work does not need an LLM at all: checking a hotel rate against a cap, matching a card charge to a receipt by amount and date, or deciding whether a report is low-risk.

## Decision

**Deterministic code handles the common case; an LLM is called only when a task needs reasoning, and its output is checked before it can change state.**

1. **A rule-based router decides, without an LLM call.** `ai_agents/orchestrator/classifier.py` maps a task's context to an agent with fixed confidence tiers (0.95, 0.80, 0.40). The orchestrator keeps the task deterministic when confidence ≥ `ROUTING_CONFIDENCE_THRESHOLD` (0.75) and the task does not require reasoning; otherwise it takes the `llm` route and dispatches to the registered agent. A missing deterministic handler returns `needs_review` and a missing agent returns `unavailable`, never an error. The orchestrator never imports an LLM provider.
2. **Inside each agent, deterministic steps come first.** The travel agent's intent parsing, inventory lookup, policy evaluation, ranking and critic are deterministic; the LLM is used for free-text slot extraction (re-validated, with a parser fallback) and an optional narrative. The expense agent's matching, categorisation, assembly and pre-submit check are deterministic; the LLM only writes an optional narrative. Receipt fields with OCR confidence ≥ 0.70 never reach the LLM.
3. **Agents ground themselves with tools** (`ai_agents/tools/`): the policy engine, inventory, itinerary and calendar, profile, policy retrieval.
4. **A critic checks reasoned output** (`ai_agents/guardrails/reflection.py` and per-agent critics). A rejected inference is nulled and marked `needs_review`; it is never committed silently.
5. **Provider failures degrade**: templated or deterministic output marked `degraded` / `needs_review`, not an error page.
6. **Every AI task writes exactly one `ai_decision_logs` row** with the route taken (`deterministic` or `llm`), provider, minimised tool and output summaries, confidence, reflection outcome, latency and the request's correlation ID.

## Alternatives considered

| Option | Why not |
|---|---|
| LLM-first agents that call tools when they decide to | Every request pays LLM latency and cost; behaviour depends on the model's choices, so tests and audits become probabilistic; a prompt can steer which tools run. |
| An LLM classifier as the router | Pays an LLM call to decide whether to make an LLM call. The classifier keeps an injectable seam so an LLM-assisted strategy can be added later without changing the orchestrator. |
| A configurable multi-agent platform where agents call each other | Hard to test and evaluate, and hard to explain in an audit. Here the orchestrator is the only component that decides which agent runs; agents share truth through tools and persisted records. |

## Consequences

**Positive**

- The routine path is fast, free and repeatable, which is what makes deterministic CI gates and byte-identical RAG regression checks possible.
- The share of AI tasks handled without an LLM can be read from `ai_decision_logs.route` (`deterministic` vs `llm`). The finance dashboard's separate automation rate (the share of approvals auto-approved) shows the same pattern from the business side.
- Outages and low-confidence results surface as `needs_review` for a human, never as a silent wrong value.

**Negative / trade-offs**

- Rules have to be written and maintained by hand; the policy engine grew rule-family tools (`entry_rules.py`, `process_rules.py`, `spend_policy.py`, `fx_policy.py`, `grade_entitlements.py`) as the policy grew.
- The rule-based classifier only understands the context types it was given; free-form "general" requests get low confidence.
- Two code paths (deterministic and LLM) per agent must both be tested.

## Evidence

- Orchestrator and classifier: [`ai_agents/orchestrator/orchestrator.py`](../../ai_agents/orchestrator/orchestrator.py), [`classifier.py`](../../ai_agents/orchestrator/classifier.py); tests `ai_agents/tests/test_orchestrator_classifier.py`, `test_orchestrator_router.py`, `test_orchestrator_db.py`.
- Critics and degradation: `ai_agents/tests/test_reflection_loop.py`, `test_openrouter_degradation.py`, `test_p6_t5_degradation_db.py`.
- Decision log: `ai_agents/decision_log/writer.py`; `ai_agents/tests/test_decision_log_writer.py`.
- Phase log: [`P4-T7_intent_classification_routing.md`](../phase_logs/P4-T7_intent_classification_routing.md). Design: [technical design § AI system design](../design/technical-design.md#ai-system-design).
