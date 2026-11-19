# Technical design

This is the technical design I wrote before building Atlas, published with notes on where the build went a different way. It is for engineers who want the reasoning behind the architecture: the four planes, the routing pattern for AI work, the module split, the security model and the API conventions. For the current map of the code, start with [ARCHITECTURE.md](../ARCHITECTURE.md); this page explains why it looks that way.

> **Original design (May 2026) — annotated with as-built notes.**
> The body text is the original design, lightly edited for a public audience. Blocks marked **As built:** describe what the code does today and were checked against the repository. Where the two disagree, the code wins. Requirement IDs (FR-…, NFR-…, US-…, M-…) refer to the [product requirements](../product/PRD.md); the [traceability matrix](../product/requirements-traceability.md) maps them to code and tests.

> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.

**Contents:** [System overview](#system-overview) · [High-level architecture](#high-level-architecture) · [AI system design](#ai-system-design) · [Backend architecture](#backend-architecture) · [Frontend architecture](#frontend-architecture) · [Database architecture](#database-architecture) · [Security model](#security-model) · [API design principles](#api-design-principles) · [Logging and monitoring](#logging-and-monitoring) · [Error handling](#error-handling)

> Design philosophy: the architecture follows one pattern throughout, *a deterministic, auditable core paired with an LLM reasoning layer that is grounded, routed and evaluated.* Every choice below favours that pattern, and favours a system small enough to build, run and reason about end to end.

---

## System overview

Atlas is a role-aware web application that connects travel planning, receipt capture, expense automation, policy enforcement and approvals into one auditable flow, with an AI orchestration layer across all of them.

Conceptually the system has four planes:

1. **Experience plane**: a React/TypeScript single-page application with role-scoped dashboards and a conversational assistant.
2. **Application plane**: a modular monolith exposing a versioned REST API, organised into bounded modules (Identity, Travel, Receipt, Expense, Policy, Approval, Analytics, AI Orchestration).
3. **Intelligence plane**: the AI orchestrator and specialised agents (Travel, Expense, Policy, Approval), the deterministic OCR/extraction core, the routing layer, the grounding/tool layer and the guardrail/reflection layer.
4. **Data plane**: one relational database (PostgreSQL), object storage for receipt images, and a vector index for policy retrieval (RAG).

The defining decision is the **deterministic-core-plus-routed-agent** pattern: cheap, fast, auditable deterministic logic handles the common case; the LLM is invoked only when reasoning is genuinely required, and its outputs are grounded and critic-checked before they affect state ([ADR-0003](../adr/0003-deterministic-core-routed-agents.md)).

> **As built:** the four planes map to directories: experience → `frontend/` (Next.js 14 App Router), application → `backend/app/`, intelligence → `ai_agents/` plus the provider adapters in `services/`, data → `database/` (PostgreSQL with pgvector, Alembic migrations 0001–0025). The vector index lives in PostgreSQL itself (pgvector, HNSW), not in a separate store.

---

## High-level architecture

### Architectural style

- **Modular monolith** with strict internal module boundaries, not microservices. It keeps the separation (clean module interfaces, one owner per table) without the operational cost of many services. Each module is designed so it *could* later be extracted behind the same REST contract ([ADR-0001](../adr/0001-modular-monolith.md)).
- **Stateless application layer**: identity and authorisation travel in signed tokens; no server-side session affinity, so the API can scale horizontally (NFR-S2).
- **Asynchronous seams for AI work**: long-running AI tasks (gap-fill, ranking, report assembly) are modelled as jobs with pending/queued states so the UI stays responsive and the work can be retried (NFR-S3, NFR-R1).

> **As built:**
> - The monolith has **fourteen** modules, not eight. The design's eight are there (`identity`, `travel`, `receipt`, `expense`, `policy`, `approval`, `analytics`, `ai`), plus `lifecycle` (reimbursement, withdrawal, trip completion), `team`, `card`, `search`, `notifications` and `audit`, all under `backend/app/modules/`.
> - Authentication is stateless (JWT). Two pieces of state live in the process: the rate-limit counters (in memory unless `RATE_LIMIT_STORE=redis`) and the optional demo-reset scheduler. Staging runs one uvicorn worker on one instance, so this is consistent there; running more instances needs the Redis store.
> - **There is no job queue.** AI work runs synchronously inside the request. Blocking provider calls are moved to the threadpool so they do not stall the event loop (for example the receipt pipeline in `backend/app/modules/receipt/router.py`), and assistant conversations stream their responses over SSE. `ocr_status` (`pending` → `processed` / `failed`) records pipeline state on the row. The README lists the missing queue as a known limitation.

### Logical components and their relationships

- **Web client** → calls → **REST API layer** → dispatches to → **domain modules**.
- **Domain modules** (Travel, Receipt, Expense, Policy, Approval, Analytics) own their data and business rules and call the **AI Orchestration module** for any reasoning task.
- **AI Orchestration module** routes to the **deterministic core** or to a **specialised agent**, which uses **tools** (internal module APIs) for grounding and passes outputs through **guardrails/reflection** before returning.
- **Data plane** is shared at the database level but **logically partitioned per module**: modules do not read each other's tables directly; they call each other's interfaces.

> **As built:** conversation, policy-query, policy-evaluate, rule-authoring and approval endpoints go through the orchestrator (`backend/app/modules/ai/runtime.py::build_orchestrator`). The receipt upload path calls the receipt pipeline and the deterministic policy engine directly, without the orchestrator, because there is no routing decision to make there. Modules share one SQLAlchemy model package, so "no cross-module table access" is a convention that the code mostly follows, not a hard wall.

### Cross-cutting concerns

Identity and RBAC, audit logging, validation, observability, error handling and the model-provider abstraction are cross-cutting and consistent across modules rather than re-implemented per module.

> **As built:** these live in `backend/app/core/` (`rbac.py`, `security.py`, `audit.py`, `errors.py`, `correlation.py`, `idempotency.py`, `concurrency.py`, `rate_limit.py`, `body_limit.py`, `demo.py`) and `ai_agents/decision_log/`. See [ARCHITECTURE.md § Cross-cutting concerns](../ARCHITECTURE.md#cross-cutting-concerns).

### Data and event flow (representative end-to-end path)

1. Employee asks the assistant to plan a trip → the Travel module receives a structured request.
2. The orchestrator routes to the **Travel Agent**, which calls the inventory tool and the Policy module's evaluation interface, ranks options and runs a reflection check.
3. Employee selects an option → a **Trip** and its **TripSegments** are persisted with an initial workflow state; an audit event is recorded.
4. Employee uploads receipts → the Receipt module runs deterministic extraction; low-confidence fields are routed to the **Receipt Analysis Agent**, grounded against the trip itinerary and calendar, critic-checked and returned with provenance.
5. The Expense module matches receipts and card transactions to the trip, creates entries, assembles a **report** and runs the pre-submit policy check.
6. On submit, the **Approval module** routes to the manager, auto-approving low-risk in-policy reports and escalating exceptions; every transition is audited.
7. Analytics reads the consolidated data for role-scoped dashboards.

> **As built:** step 4 also auto-drafts an expense line into a "Receipt Inbox" draft report with a single-entry policy verdict, inside a savepoint so a drafting failure never fails the upload. In step 6, submitting a report or trip opens its approval **in the same transaction** (`backend/app/modules/approval/lifecycle.py::open_approval_on_submission`), so a submitted subject never exists without an approval. Auto-approval requires an in-policy result, zero blocking flags, no required approver grade and a known total of at most 500 USD. The full sequence is drawn in [ARCHITECTURE.md § Expense lifecycle](../ARCHITECTURE.md#expense-lifecycle-end-to-end).

---

## AI system design

The intelligence plane is organised as **one orchestrator over a small set of specialised agents**, each backed by tools and guardrails. It is deliberately *not* a configurable multi-agent platform; a fixed set of agents is easier to test, evaluate and debug.

### Shared AI infrastructure

- **Orchestrator / router (the front door).** Classifies each task by intent and **routes** it: to the deterministic path when confidence is high and no reasoning is needed, or to the appropriate agent when reasoning is required. Routing is the system's main control for cost, latency and reliability (FR-A1, NFR-P3).
- **Tool layer (grounding).** Agents never act on model memory alone; they call internal tools (Inventory, Policy Evaluation, Policy Retrieval, User Profile/History, Itinerary/Calendar) to fetch ground truth (FR-A2): fetch the truth rather than hallucinate it.
- **Reflection / critic layer (safety).** Reasoning outputs (recommendations, inferred fields) go through a generator → critic loop that checks the hypothesis against known facts before commit (FR-A3). The motivating example is the "Paris paradox": a receipt that reads like Paris, France, while the itinerary shows a Dallas-area trip, should be corrected to Paris, Texas rather than accepted.
- **Grounding and citation contract.** Every reasoned output carries provenance (a policy clause reference, a source field, or the tool result it relied on) plus a confidence indicator (FR-A4).
- **Model-provider abstraction.** All agents call the model through one provider interface, so the underlying LLM is swappable (FR-A6). The model is a dependency, not the architecture.
- **Decision logging.** Every AI invocation records inputs, route chosen, tools called, output, confidence and critic result, for audit and evaluation (FR-A5).

> **As built:**
> - **Classifier** (`ai_agents/orchestrator/classifier.py`): rule-based, no LLM call. Fixed confidence tiers (0.95, 0.80, 0.40) by context type; the orchestrator keeps a task deterministic when confidence ≥ `ROUTING_CONFIDENCE_THRESHOLD` (0.75) and the task does not require reasoning. With no registered handler it returns `needs_review` instead of failing.
> - **Tools** (`ai_agents/tools/`): the five designed tools exist (`inventory.py`, `policy_eval.py`, `policy_retrieval.py`, `profile.py`, `itinerary_calendar.py`), plus rule-family evaluators added as the policy grew: `entry_rules.py`, `process_rules.py`, `spend_policy.py`, `fx_policy.py`, `grade_entitlements.py`, `ground_transport.py`, `report_totals.py`.
> - **Critics:** receipt location reflection (`ai_agents/receipt_pipeline/reflection.py`), travel policy-honesty critic (`travel_agent/reflection.py`), expense match-honesty critic (`expense_agent/reflection.py`), rule-authoring critic (`ai_agents/agents/policy_authoring.py`), and for policy answers the citation contract (`ai_agents/guardrails/citations.py`) plus a per-sentence support gate (`ai_agents/rag/support.py`).
> - **Providers:** `LLM_PROVIDER` is `local`, `openrouter` or `anthropic`; staging uses OpenRouter with Gemini 2.5 Flash and Gemini 2.5 Flash Lite as fallback. OCR, embeddings, rerank, storage and email have their own adapters ([ADR-0002](../adr/0002-provider-adapters-with-local-fallbacks.md)).
> - **Decision log:** one `ai_decision_logs` row per AI task, minimised for secrets and PII by `ai_agents/decision_log/minimizer.py`. A `correlation_id` column (migration 0007) ties each row to the HTTP request.

### Travel Agent

- **Responsibility:** turn natural-language travel intent into a ranked, policy-checked, explained recommendation.
- **Behaviour:** ReAct-style loop. Parse intent, identify missing slots, ask one clarifying question if needed, call the Inventory tool, call the Policy Evaluation tool per candidate, rank against competing objectives (price, policy, preference, sustainability) and produce rationales.
- **Tools:** Inventory, Policy Evaluation, User Profile/History.
- **Guardrails:** a reflection check that no recommended option silently violates policy; refusal to invent fares or options that are not in inventory.
- **Output:** a shortlist with per-option in/out-of-policy labels, reasons and plain-language trade-offs.

> **As built** (`ai_agents/agents/travel_agent/`): every step is deterministic except two optional LLM uses: slot extraction from free text (with a real provider only, re-validated and falling back to a deterministic parser) and the explanation narrative. Ranking puts compliance above price; out-of-policy options are labelled, never hidden. On a provider error the agent falls back to templated explanations and marks the result `needs_review`. Inventory is the seeded mock in `services/mock_inventory/`.

### Expense Agent

- **Responsibility:** assemble expense entries and reports from captured signals with minimal user effort.
- **Behaviour:** smart matching (entity resolution across transaction ↔ receipt ↔ trip segment), auto-categorisation and itemisation, then report assembly.
- **Tools:** Itinerary, receipt data, simulated card transactions, Policy Evaluation (for the pre-submit check).
- **Guardrails:** duplicate detection; pre-submit policy validation with explanations; the user always reviews before submit.
- **Output:** a draft report with matched, categorised entries and a pre-submit flag list.

> **As built** (`ai_agents/agents/expense_agent/`): ingest, smart-match, rule-first categorisation, assembly, pre-submit check and the match-honesty critic are all deterministic; the only LLM step is an optional narrative (`explain_with_llm`). The same pre-submit engine (`presubmit.py::run_presubmit`) is reused by the receipt upload path, the report editor and the approval agent, so the user and the approver see the same verdict.

### Policy Agent

- **Responsibility:** answer "is this allowed?" and evaluate items against policy.
- **Two complementary mechanisms:**
  - **Deterministic rule evaluation**: structured rules (caps, categories, per diems, preferred suppliers) evaluated as data; fast, auditable, no LLM needed.
  - **Grounded policy Q&A (RAG)**: retrieve relevant clauses from the vector index and answer with citations; say "policy is silent" when nothing relevant is retrieved (US-E2).
- **Rule-authoring assist:** translate near-natural-language rule intent from finance into a structured, testable rule for review (US-F1), never activated without human confirmation.
- **Guardrails:** answers must cite a retrieved clause; no clause, no confident claim.

> **As built:** the rule engine (`ai_agents/tools/policy_eval.py` and the rule-family tools) evaluates 24 active structured rules covering 29 clauses of the synthetic Helios Dynamics TEP-FY26 policy. Q&A runs on the Atlas Retrieval Engine: dense and sparse retrieval, Reciprocal Rank Fusion, cross-encoder rerank and calibrated abstention, which makes no LLM call when it abstains ([RAG_ARCHITECTURE.md](../RAG/RAG_ARCHITECTURE.md)). Rule authoring is LLM draft → critic → saved as `draft` → a finance admin activates it.

### Approval Agent

- **Responsibility:** route approvals, auto-approve low-risk in-policy items, escalate exceptions, keep humans on the loop.
- **Behaviour:** evaluate risk and policy status; if fully in-policy and low-risk, auto-approve with a recorded rationale; otherwise route to the human approver with the flagged reasons and context.
- **Tools:** Policy Evaluation, approval workflow state.
- **Guardrails:** the agent can only auto-*approve* within configured low-risk bounds; it never auto-rejects and never exceeds the permissions of the workflow it serves (NFR-SEC6).

> **As built:** the agent uses **no LLM at all** ([ADR-0004](../adr/0004-llm-free-approval-agent.md)). It re-runs the policy engine over the subject's current lines, adds segregation-of-duties checks (a re-filed returned or rejected claim is flagged `refiled_returned_claim` / `refiled_rejected_claim`), and auto-approves only when every gate in `ai_agents/agents/approval_agent/routing.py::decide_route` passes, with `LOW_RISK_AUTO_APPROVE_MAX_USD = 500.00`. Anything else is escalated and routed by approver grade where a flag requires one (`backend/app/modules/approval/grade_routing.py`). A human can still decide on an auto-approved item (`_DECIDABLE_STATUSES` includes `auto_approved`).

### How the agents relate

The orchestrator is the only component that decides *which* agent runs; agents do not call each other. Shared truth flows through tools and persisted records, not agent-to-agent messages, which keeps the system debuggable and auditable.

> **As built:** this holds. Agents share code (the approval agent imports the expense agent's pre-submit functions) but never invoke another agent.

---

## Backend architecture

### API structure

- One **versioned REST API** (`/api/v1/...`) fronting the modular monolith.
- **Resource-oriented** design: nouns as resources, HTTP verbs for actions, sub-resources for relationships.
- **Consistent envelope:** standard success and error shapes, pagination, filtering and sorting conventions applied uniformly.
- **Streaming endpoints** for the conversational assistant, so responses stream token by token (NFR-P2).

> **As built:** 80 endpoints under `/api/v1` plus `GET /health`, all registered in `backend/tests/api/test_contract_matrix.py`. Errors share one envelope (`{"error": {"code", "message", "correlation_id"}}`, `backend/app/core/errors.py`); collections return `{items, total, page, size, pages}` (`backend/app/core/pagination.py`). `POST /api/v1/conversations/{id}/messages` streams over SSE; the browser reads it with `fetch` and a stream reader because the request is a POST (`frontend/lib/api/stream.ts`).

### Service separation (modules within the monolith)

Each module owns its domain logic and data and exposes an internal interface:

- **Identity and access**: users, roles, permissions, authentication, RBAC enforcement.
- **Travel**: trips, segments, planning requests, recommendation persistence.
- **Receipt**: image intake, extraction results, field provenance and confidence.
- **Expense**: entries, reports, smart matching, itemisation.
- **Policy**: policy documents, structured rules, rule versions, evaluation interface, retrieval index.
- **Approval**: workflows, steps, routing, decisions, status.
- **Analytics**: read-optimised aggregations for dashboards.
- **AI orchestration**: orchestrator, agents, tool registry, guardrails, decision logs, model-provider adapter.

> **As built:** see the code map in [ARCHITECTURE.md](../ARCHITECTURE.md#code-map). AI orchestration is split between `backend/app/modules/ai/` (endpoints and the composition root) and the `ai_agents/` package (everything else).

### Authentication system

- **Token-based authentication**: short-lived signed access tokens, refresh on expiry.
- **Server-side RBAC** on every request; the client's claimed role is never trusted (NFR-SEC2).
- **Record-level scoping** in the data-access layer, so queries are constrained to what the caller may see: employee → self; manager → direct reports; finance → organisation (NFR-SEC3).
- **Multi-tenant seam:** all tenant-scoped tables carry a tenant key even though the build runs single-tenant, so tenant isolation can be switched on later (NFR-S4).

> **As built:**
> - Access tokens last 15 minutes and refresh tokens 7 days (HS256, `backend/app/core/security.py`). A keyed fingerprint of `users.password_changed_at` in each token revokes older sessions after a password reset or change.
> - Permissions are resolved from `role_permissions` in the database on each request (`rbac.py::get_user_permissions`); `scope_query` applies the tenant filter first, then the role scope. Writes are owner-only for every role (`require_owner`).
> - **Multi-tenancy is live, not just a seam.** Login takes an optional organisation code (`organizations.slug`, migration 0022), email is unique per organisation, and tests run with two organisations. Isolation is application-level; there is no PostgreSQL row-level security ([ADR-0006](../adr/0006-application-level-tenancy.md)).
> - Account recovery (password reset, invitations, email verification) was added later with one-time tokens stored as SHA-256 digests (`auth_tokens`, migration 0024). See the [security model](security-model.md#account-recovery-tokens).

---

## Frontend architecture

### Dashboard structure

A single-page application with a persistent shell (top bar and left sidebar) and a routed content area. The shell adapts to the authenticated role, exposing only the navigation and data the role is authorised to see.

### Component hierarchy (conceptual)

- **App shell**: auth/session context, global navigation, notifications.
  - **Sidebar navigation**: role-aware links (Planner, Trips, Receipts, Expenses, Approvals, Analytics, Policy/Admin).
  - **Route views**, one per primary screen: Travel Planner (conversation panel + results panel), Trips (list + detail), Receipts (upload zone + extraction review with provenance badges), Expenses (report list + detail with entries, matches and pre-submit flags), Approvals (queue + decision panel), Analytics (role-scoped widgets), Policy/Admin (policy document, rule list, rule-authoring assist; finance only).
  - **Assistant surface**: a conversational component reusable across views.
- **Shared primitives**: buttons, cards, tables, forms, modals, badges, toasts (see the [UI/UX design](ui-ux.md)).

### State and data handling (conceptual)

- **Server state** (trips, expenses, approvals) is fetched and cached per view, with clear loading, empty and error states.
- **Conversation state** is kept per assistant session and tied to a persisted conversation record, so context survives reloads.
- **Optimistic, reversible UI** for low-risk actions; explicit confirmation for state-changing actions (submit, approve, reject).

> **As built:** Next.js 14 App Router with TypeScript, Tailwind CSS and shadcn/ui, TanStack Query for server state and Zustand for client state (`frontend/stores/`). Navigation is driven by permission keys and roles in `frontend/lib/nav-config.ts`, which also gates routes; the server re-checks everything. The shell adds a notification bell, global search and an assistant drawer. Screen-by-screen notes are in [ui-ux.md](ui-ux.md).

---

## Database architecture

> The full entity and field design is in [database-schema.md](database-schema.md). This section covers architecture-level decisions only.

### Storage strategy

- **PostgreSQL** as the single system of record: relational integrity for the trip ↔ expense ↔ approval relationships, transactional guarantees for workflow state, and JSON support for semi-structured AI artifacts.
- **Object storage** for receipt images; the database stores references and extracted fields, never the binary.
- **Vector index** for policy retrieval, either a PostgreSQL vector extension or a lightweight embedded index; co-locating with PostgreSQL keeps the footprint small.
- **Append-only audit store** as a dedicated table family rather than mutating history in place.

> **As built:** pgvector with an HNSW index (`ef_construction = 200` since migration 0012) plus a `tsvector` column with a GIN index for the lexical leg of hybrid retrieval (migration 0010). Receipt rows store an object key in `receipts.image_url`, never a public URL. The audit store is one table, `audit_log`, append-only by convention (a single writer, no database trigger), alongside `ai_decision_logs`.

### Data flow principles

- **Capture at the source** (booking, receipt, simulated card), so downstream entities are derived, not re-keyed.
- **Derived but traceable:** auto-created expense entries always link back to their source receipt or transaction and the trip that grounds them.
- **State as data:** workflow status lives in explicit state columns plus an immutable transition log, never inferred.
- **Module-owned tables:** each module reads and writes only its own tables; cross-module needs go through interfaces.

> **As built:** lineage is visible in the UI (receipt → entry → report → trip) and extended by migration 0023, which records where a line moved out of a withdrawn report came from (`expenses.released_from_report_id`, `released_from_approval_id`, `released_at`). Status columns are PostgreSQL enums; transitions are enforced in the services and recorded in `audit_log` and `approval_steps`.

---

## Security model

### RBAC

- **Roles:** Employee, Manager, Finance/Admin.
- **Permissions** are granted to roles through a role ↔ permission mapping, so permissions can evolve without code changes to checks.
- **Enforcement points:** every API request passes authentication → role/permission check → record-level scope filter, all server-side and mandatory (NFR-SEC1–3).
- **Scoping rules:**
  - Employee: own trips, receipts, expenses and conversations only.
  - Manager: the above for self, **plus** read and decision access to direct reports' submitted items.
  - Finance/Admin: organisation-wide read, policy management, audit, and override (with a recorded reason).

> **As built:** as designed, with two additions: segregation of duties (nobody decides or reimburses their own claim, for any role) and approver grades (a flag that names an SVP or VP approver can only be decided by a user of that grade or above). Direct reports are one level deep (`rbac.py::direct_report_ids`).

### Data protection

- **In transit:** all traffic over TLS.
- **At rest:** database and object storage encrypted; receipt images served through short-lived, scoped access references rather than public links (NFR-SEC4).
- **PII minimisation:** only the grounding context a given AI task needs is sent to the model; secrets and unrelated PII are excluded from prompts (NFR-SEC5).
- **AI action safety:** the assistant can never perform a state-changing action the caller's role does not permit, and inputs are guarded against prompt-injection-driven actions (NFR-SEC6).
- **Auditability:** every access decision and override is traceable through the audit trail.

> **As built:** TLS is provided by Vercel and Render; staging and production refuse a non-https `APP_BASE_URL`. Encryption at rest relies on the managed services (Neon, Cloudflare R2); nothing in this repository configures it. Receipt images are served only through signed URLs capped at 300 seconds, after RBAC and scope checks. The decision-log minimiser redacts secret-named keys, bearer tokens, email addresses and card-like numbers before persistence. Agent actions are a fixed enum mapped to permission keys (`ai_agents/guardrails/actions.py`). The consolidated view is in [security-model.md](security-model.md).

---

## API design principles

> Conceptual only: resource shapes and verbs, not implementations.

### REST principles

- **Resource-oriented:** nouns (trips, receipts, expenses, reports, approvals, policies, conversations) as resources.
- **Standard verbs:** `GET` (read), `POST` (create), `PATCH` (partial update or state transition), `DELETE` (soft-delete where allowed). No verbs in paths.
- **Statelessness:** every request carries its own auth; no server session state.
- **Versioned:** prefix with `/api/v1` so the contract can evolve.
- **Consistent envelopes, pagination, filtering and error format** across all resources.
- **Idempotency** for create and upload operations, so retries do not create duplicates (NFR-R3).
- **HATEOAS-lite:** responses include the related-resource links a client needs.

> **As built:**
> - **No soft delete.** A `SoftDeleteMixin` exists in `database/models/mixins.py`, but no model uses it. `DELETE` exists only where hard deletion is safe: a never-submitted draft report (refused with `409 REPORT_HAS_APPROVAL` once it has approval history), a draft entry, and an unused receipt. Records with review history are kept.
> - A few lifecycle actions are explicit `POST` sub-resources rather than `PATCH` transitions, because they carry their own rules: `/expense-reports/{id}/reimburse`, `/withdraw`, `/trips/{id}/withdraw`, `/trips/{id}/complete`.
> - Idempotency: `Idempotency-Key` header, stored in `idempotency_keys` with a unique `(organization_id, scope, idempotency_key)` constraint ([ADR-0007](../adr/0007-optimistic-concurrency-and-idempotency-keys.md)). Optimistic concurrency: `If-Match` carries the integer `version`.
> - HATEOAS-lite links were not implemented; the frontend builds URLs from IDs.

### Endpoint structure

The original conceptual map:

- **Identity:** `POST /auth/login`, `POST /auth/refresh`, `GET /me`
- **Users (admin):** `GET /users`, `GET /users/{id}`
- **Travel planning (assistant):** `POST /conversations`, `POST /conversations/{id}/messages` (streaming), `GET /conversations/{id}`
- **Trips:** `GET /trips`, `POST /trips`, `GET /trips/{id}`, `PATCH /trips/{id}`, `GET /trips/{id}/segments`
- **Receipts:** `POST /receipts` (idempotent upload), `GET /receipts/{id}`, `PATCH /receipts/{id}` (field corrections)
- **Expenses:** `GET /expense-reports`, `POST /expense-reports`, `GET /expense-reports/{id}`, `PATCH /expense-reports/{id}`, `GET /expense-reports/{id}/entries`, `PATCH /expenses/{id}`
- **Policy:** `GET /policies`, `GET /policies/{id}/rules`, `POST /policies/{id}/rules` (rule-authoring draft), `POST /policy/evaluate`, `POST /policy/query` (grounded Q&A)
- **Approvals:** `GET /approvals`, `GET /approvals/{id}`, `PATCH /approvals/{id}` (approve / reject with reason / request changes)
- **Analytics:** `GET /analytics/overview`
- **Audit:** `GET /audit`

All collection endpoints support filtering, sorting and pagination; all state-changing endpoints emit audit events.

> **As built:** every designed endpoint exists except `GET /policies/{id}/rules`, which became `GET /policy-rules`. The 80-endpoint surface adds:
>
> | Area | Added endpoints (all under `/api/v1`) |
> |---|---|
> | Identity and accounts | `GET /organization`, `GET /grades`, `POST /users`, `PATCH /users/{id}`, `POST /users/{id}/deactivate`, `/reactivate`, `POST /auth/forgot-password`, `/reset-password`, `/change-password`, `/send-verification`, `/verify-email`, `GET /auth/invite/{token}`, `POST /auth/accept-invite`, `POST /users/invite`, `/users/{id}/invite/resend`, `/revoke` |
> | Receipts | `GET /receipts` (attachable receipts), `DELETE /receipts/{id}` |
> | Expenses | `PATCH /expense-reports/{id}/details` (rename a draft), `POST /expense-reports/{id}/entries/move`, `POST /expense-reports/{id}/mileage`, `POST /expenses/{id}/receipts`, `DELETE /expenses/{id}`, `DELETE /expense-reports/{id}`, `GET /mileage-rate` |
> | Lifecycle | `GET /reimbursements/queue`, `POST /expense-reports/{id}/reimburse`, `/withdraw`, `POST /trips/{id}/withdraw`, `/complete` |
> | Approvals | `POST /approvals` (manual trigger; submit now opens the approval itself), `GET /approvals/exception-types`, `/departments`, `/export.csv` |
> | Policy | `GET /policy-rules`, `PATCH /policy-rules/{id}`, `GET /policy/rules/{id}/history` |
> | Shell and team | `GET /notifications`, `/unread-count`, `POST /notifications/read-all`, `/{id}/read`, `GET /search`, `GET /team/members`, `/team/members/{id}`, `GET /card-transactions`, `GET /audit/export.csv` |
> | Operations | `GET /ops/rate-limits`, `GET /ops/demo-reset` (finance admin), `GET /public/config` (unauthenticated, non-secret flags) |

---

## Logging and monitoring

### Application logging

- **Structured logs** with correlation IDs that tie a user action → API request → AI invocation → state change together across the request lifecycle.
- **Severity discipline:** debug/info/warn/error kept separate; no sensitive data in logs.

> **As built:** JSON logs by default (`LOG_FORMAT=json`, `backend/app/core/logging.py`) with the correlation ID from `backend/app/core/correlation.py`. Audit payloads, tokens, passwords and presigned URLs are never logged; the database host is logged, never the DSN.

### AI observability

- A dedicated **AI decision log** records, per invocation: task type, route (deterministic or agent), tools called and their results, model/provider used, output, confidence and the critic outcome (FR-A5). This is what makes the system explainable and auditable.
- **Evaluation hooks:** the decision log doubles as a dataset for measuring extraction accuracy, match accuracy and policy-decision correctness (M-A1–3).

> **As built:** the decision log exists as designed. Accuracy is measured on curated datasets in `evals/datasets/` with floors in `evals/thresholds.py`, run by the CI `eval-db` job and the manual live evals, rather than mined from production decision logs. Results are in the [README](../../README.md#quality--evaluation) and [FINAL_METRICS_SUMMARY.md](../RAG/metrics/FINAL_METRICS_SUMMARY.md).

### Audit trail

An **append-only audit table** records who did what, when, to which entity, with before/after state, for every meaningful business event (trip created, report submitted, approval decided, policy rule changed, override applied). Immutable by design (NFR-R4).

> **As built:** `audit_log`, written only through `backend/app/core/audit.py::record_audit` in the same transaction as the change. Immutability is a convention (the application never updates or deletes rows); there is no database trigger. Refused admin actions (for example a protected demo account edit) are audited too.

### Monitoring

- **Health checks** for the app and its dependencies (database, object storage, model provider).
- **Key signals:** request latency and error rate, AI path mix (deterministic vs LLM, M-R4), LLM latency/error/timeout rate, and queue depth for async AI jobs.
- **Alerting seams** defined conceptually (error-rate and AI-failure-rate thresholds).

> **As built:** `GET /health` is a liveness probe that reports `status`, `app`, `env`, `version` and `commit`; it does not check dependencies. The database is checked once at startup, and the app refuses to start if it is unreachable. `GET /api/v1/ops/rate-limits` reports rate-limit soft signals, and the finance analytics page shows the automation rate (the share of approvals that were auto-approved). The deterministic-vs-LLM mix is recorded per task in `ai_decision_logs.route` but has no dashboard. No alerting is wired in this repository.

---

## Error handling

### Principles

- **Fail safe, not silent.** Errors surface clearly with actionable messages; nothing is partially committed.
- **Consistent error contract.** All API errors share one shape (code, message, correlation ID) so the client handles them uniformly.
- **Validate at the boundary.** Inputs are validated at the API edge; invalid AI tool inputs and outputs are rejected by the guardrail layer.

### AI-specific resilience (graceful degradation)

- **LLM failure or timeout → deterministic fallback.** If an agent path errors or times out, the system returns the deterministic result and marks affected fields or recommendations "needs review" rather than failing the whole flow (NFR-R1).
- **Low confidence → human review, never silent commit.** Below-threshold inferences are flagged for the user.
- **Reflection rejection → safe default.** If the critic rejects a hypothesis, the system asks for clarification or defers to deterministic output instead of guessing.

> **As built:** as designed. Examples: a rejected inferred receipt location is set to null with `needs_review`; a receipt total the provider marks as illegible or ambiguous is withheld rather than gap-filled; a rerank failure falls back to RRF order; a failed approval-agent run opens a pending, not-evaluated approval instead of blocking the submit.

### Transactional and idempotency safeguards

- **Atomic state transitions**, so a trip, report or approval is never left half-updated (NFR-R2).
- **Idempotent uploads and creations**, so retries do not create duplicate receipts or entries (NFR-R3).
- **Compensating actions** for multi-step flows: if report assembly fails midway, partial entries are rolled back or left as draft, never submitted.

> **As built:** status transitions are a compare-and-swap `UPDATE ... WHERE version = :expected` (`backend/app/core/concurrency.py::guarded_version_update`) that returns 409 on a stale version. The business change, its audit row and its idempotency key are committed together. Optional steps run in savepoints (receipt auto-draft, pre-submit refresh), so their failure degrades the response instead of rolling back the main change.
