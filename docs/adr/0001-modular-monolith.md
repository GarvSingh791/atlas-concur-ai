# 0001 — Modular monolith with one-way package layering

- **Status:** Accepted
- **Date:** 2026-05-31 (repository layout, phase P1-T1); layering rule enforced by a test from phase 4
- **Decider:** Garv Singh Saini

## Context

Atlas covers identity, travel, receipts, expenses, policy, approvals, analytics and an AI layer. The domains are tightly connected: submitting a report must open an approval in the same transaction; an approval decision changes the report; every change writes an audit row. The system is built and operated by one engineer, deployed to free-tier hosting (one Render container, one Neon database), and must run end to end on a laptop and in CI without network access.

The question was how to split the code: separate services per domain, or one deployable with internal boundaries.

## Decision

Build **one FastAPI application with explicit internal modules**, and split the code into four Python packages with dependencies that point one way:

```text
config  <-  services  <-  ai_agents  <-  backend
                \__ database.models (depends only on config) __/
```

- `backend/app/modules/` holds fourteen domain modules, each with its own `router.py`, `service.py` and `schemas.py`. Cross-cutting code lives in `backend/app/core/`.
- `ai_agents/` (orchestrator, agents, RAG engine, guardrails, decision log) never imports `backend`. `backend/app/modules/ai/runtime.py` is the composition root that wires the agents into the orchestrator.
- `services/` (provider adapters) imports only `config`.
- One PostgreSQL database and one transaction per request; the endpoint owns `session.commit()`, and library code only flushes.

## Alternatives considered

| Option | Why not |
|---|---|
| Microservices per domain | The core flows are cross-domain and transactional (submit → approval → audit). Splitting them would need sagas or an outbox, a message broker and several deployables, none of which fit one engineer and a free hosting tier. |
| A single flat package with no internal boundaries | Fast at first, but the AI layer would end up importing web code, and it would no longer be testable on its own. The layering is what lets `ai_agents` run hermetically in its own test suite. |
| Separate deployable for the AI layer | Gives independent scaling, but every agent needs the same database session and transaction as the business change it accompanies. Keeping it in process keeps "change + audit row + decision-log row" atomic. |

## Consequences

**Positive**

- One deploy, one transaction boundary, one database: a submit, its approval, its audit row and its decision-log row commit together or not at all.
- `ai_agents` can be imported, tested and evaluated without FastAPI.
- A module could later be extracted behind its existing REST routes; the HTTP contract is already versioned (`/api/v1`) and covered by the contract matrix.

**Negative / trade-offs**

- All modules share one SQLAlchemy model package, so "a module only touches its own tables" is a convention, not a wall. Some modules read others' tables directly; for example the analytics service queries approvals, expense reports, receipts and trips for its aggregates.
- One process means one blast radius: a slow provider call ties up a worker. Blocking calls are moved to the threadpool, and there is no job queue (listed as a known limitation).
- Scaling is vertical or by replicating the whole app; state kept in process (rate-limit counters, the demo-reset scheduler) then needs the optional Redis store.

## Evidence

- Layering test: [`ai_agents/tests/test_no_backend_import.py`](../../ai_agents/tests/test_no_backend_import.py) imports the package's core modules (orchestrator, tools, RAG engine, guardrails, decision log, policy and expense agents) in a subprocess and fails if any `backend.*` module loads; no other module in the package imports `backend`. The only exception is a deferred import inside the `ai_agents/rag/reindex.py` CLI `main()`.
- Composition root: [`backend/app/modules/ai/runtime.py`](../../backend/app/modules/ai/runtime.py); router wiring: [`backend/app/api/v1.py`](../../backend/app/api/v1.py).
- Same-transaction submit: `backend/app/modules/approval/lifecycle.py::open_approval_on_submission`, called from the expense and travel services.
- Code map: [ARCHITECTURE.md § Code map](../ARCHITECTURE.md#code-map); phase log [`Phase1_P1-T1.md`](../phase_logs/Phase1_P1-T1.md).
