# Atlas: Product Requirements Document

This is the product spec I wrote before building Atlas: the vision, the problem, the three user roles, the MVP scope, user stories, functional and non-functional requirements, and success metrics. It is for anyone who wants to know what Atlas was meant to do before reading how it was built.

| | |
|---|---|
| **Status** | Original pre-build product spec (May 2026), kept as written apart from light edits for publication |
| **What shipped** | See [requirements-traceability.md](requirements-traceability.md): every requirement below, with its as-built status and evidence |
| **Domain background** | [domain-study-sap-concur.md](domain-study-sap-concur.md) and [concur-to-atlas-map.md](concur-to-atlas-map.md) |
| **Author** | Garv Singh Saini |

> [!NOTE]
> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.

**Edits made for publication.** I removed lines that only made sense in my private working notes (audience and source-of-truth lines), replaced references to my private research notes with links to the public [domain study](domain-study-sap-concur.md), and gave the MVP bullets and out-of-scope bullets IDs (`MVP-01`…, `OOS-01`…) so the traceability matrix can point at them. I also tidied punctuation and wording, and described the analogue products generically in the feature headings. The requirements themselves are unchanged. Where the build later differed, that is recorded in the traceability matrix, not here.

---

**Product (working name):** Atlas, an AI-powered Travel & Expense copilot
**Domain:** corporate Travel & Expense (T&E), studied through SAP Concur's public product material and its announced Joule agents
**Document type:** product definition only (no code, no implementation steps, no timelines)

> Scope note: every feature in this PRD traces to a capability described in the [domain study](domain-study-sap-concur.md): conversational, policy-aware booking; expense automation; receipt capture with agentic gap-filling; a policy compliance engine; pre-trip and report approval workflows; and the analytics/dashboard layer. No product capability is introduced beyond what the study supports.

---

## Product Vision

Atlas is a connected, policy-aware Travel & Expense copilot that turns the three most disliked corporate spend tasks (booking a trip, capturing receipts, and filing an expense report) into a single conversational, mostly automated flow with control and visibility built in.

The vision follows the direction the category is publicly moving in: from *embedded machine learning running silently in the background* to *agentic AI that reasons, calls tools, grounds itself in the traveler's own context, and takes multi-step action with a human kept on the loop.* Atlas is a small, self-contained analogue of that direction, where the booking event, the receipt event, and the reporting event are linked so that data from one makes the others smarter, and where policy is enforced *before* the spend rather than discovered weeks later.

The product succeeds when an employee can say "book me a trip to San Francisco next Tuesday under policy," receive a compliant, explained recommendation, travel, photograph their receipts, and have a complete, pre-validated expense report waiting for them, with the manager and finance team seeing the right slice of the truth at the right time.

---

## Problem Statement

Corporate travel and expense is structurally painful for everyone involved. The domain study identifies the same root causes that T&E suites such as SAP Concur were built to solve:

1. **Spend leakage and no real-time visibility.** Finance cannot see committed travel spend until expense reports trickle in weeks later. Spend must be captured at the source (booking, receipt) to be controllable.
2. **Manual, error-prone expense reporting.** Filling in reports is one of the most disliked recurring corporate tasks; manual entry produces errors, missing receipts, and delays.
3. **Policy compliance and audit risk.** Companies maintain detailed policies (cabin class, hotel caps, per diems, preferred suppliers). Catching violations after reimbursement is expensive; preventing them at the point of booking or submission is far cheaper.
4. **Fragmentation and context-switching.** Booking tools, card data, receipts, and reports live in disconnected places, forcing re-keying and reconciliation.
5. **OCR alone is not enough.** Traditional receipt scanning stalls the moment a field is missing or ambiguous, dumping the work back on the user. This is the gap that agentic reasoning (grounding against trip and calendar context) is meant to close.

Atlas addresses these by (a) making travel planning conversational and policy-grounded, (b) automating receipt-to-expense creation including intelligent gap-filling, (c) enforcing policy as a first-class engine, and (d) routing approvals with low-risk auto-approval and exception escalation.

---

## Target Users

Atlas serves the three roles that appear throughout the domain: the traveler, the approver and the finance team. Each role sees a different slice of the same connected data.

### Employees (Travelers / Submitters)
The people who travel and spend. They want to book a compliant trip quickly, avoid manual data entry, and get reimbursed fast. They mainly use the conversational assistant, the receipt scanner, and their own expense reports. The main thing to design against is friction fatigue: every extra field is a tax.

### Managers (Approvers)
Team leads who approve trips and expense reports for their direct reports. They want fast, confident decisions: what is in or out of policy, why it was flagged, and the relevant context, without becoming a bottleneck. They benefit most from auto-approval of low-risk items and clear escalation of exceptions.

### Finance Teams (Administrators / Auditors)
Finance and travel-program owners. They own policy, audit, budgets, and reporting. They want near-real-time visibility into spend, high automation rates, low exception and error rates, and a complete audit trail. They configure policy rules and review flagged items at scale.

---

## Core Features

Each feature below is an analogue of a publicly described T&E capability (see [concur-to-atlas-map.md](concur-to-atlas-map.md)).

### 1. AI Travel Planner (analogue of a conversational, policy-aware booking agent)
A conversational assistant that accepts natural-language travel requests, clarifies ambiguous intent, retrieves options from a travel inventory (mocked for this build, because live GDS/NDC access is not realistic for an independent project), ranks them against competing objectives (price, policy compliance, traveler preference, sustainability), and recommends a compliant itinerary with a plain-language explanation of tradeoffs. Policy is enforced *inside* the planning flow, not afterward.

### 2. Expense Automation (analogue of expense-report automation)
Automatic creation and population of expense entries and reports from captured signals (receipts, simulated card transactions, and the traveler's trip itinerary), using smart matching to link a charge ↔ receipt ↔ trip line item. The employee's role shifts from data entry to *review and submit.*

### 3. Receipt Scanner (analogue of receipt capture with an analysis agent)
Upload or photograph a receipt; a deterministic OCR/extraction core lifts high-confidence fields; when a field is missing or low-confidence, the request is routed to an AI agent that infers the missing value from grounding context (the traveler's trip itinerary and calendar) and self-checks its hypothesis before committing. This is the "deterministic core + routed agent + reflection" pattern, which avoids the "Paris paradox" (a receipt that looks like Paris, France when the itinerary says Paris, Texas).

### 4. Policy Compliance Assistant (analogue of audit rules, policy Q&A and AI rule authoring)
A policy engine combining (a) deterministic, configurable rules (caps, allowed categories, preferred suppliers, per diems, required receipts) and (b) grounded policy Q&A via RAG that answers "is this allowed?" with citations to the specific policy clause. Enforcement is shifted "left": at booking time and at pre-submit, not only at reimbursement.

### 5. Approval Workflow System (analogue of pre-trip requests and pre-submit review)
A routing engine that sends trips and expense reports to the right approver, auto-approves low-risk in-policy items, escalates exceptions with the reason and supporting context, and tracks status end-to-end as an auditable trail. Humans stay on the loop (review and approve), not in the loop (do every step by hand).

### 6. Role-aware Dashboard (analogue of T&E analytics and budget views)
A role-aware dashboard that gives each user the correct slice: employees see their trips, expenses and reimbursement status; managers see pending approvals and team spend; finance sees consolidated spend, automation rate, exception rate, and budget posture.

---

## MVP Scope

The MVP is a **single connected vertical slice** that proves the core idea end-to-end for one fictional company with seeded users, one travel policy document, and a mock travel inventory. It is intentionally narrow but complete.

**In scope for MVP:**

- **MVP-01 Authentication & RBAC** for three roles (Employee, Manager, Finance/Admin) with role-scoped data access.
- **MVP-02 Conversational Travel Planner** over a mock inventory: natural-language request → clarification → ranked, policy-checked recommendations → "select" to create a trip record. Single traveler, domestic and simple international trips (flight + hotel).
- **MVP-03 Policy Compliance Engine:** deterministic rule evaluation plus grounded policy Q&A (RAG with citations) over one seeded company policy.
- **MVP-04 Receipt Scanner:** image upload → deterministic extraction → confidence-based routing → agentic gap-fill grounded in the trip itinerary → editable result.
- **MVP-05 Expense Automation:** auto-create expense entries from receipts and trip context, smart-match to a trip, assemble into an expense report for review and submission.
- **MVP-06 Approval Workflow:** single-level approval routing with low-risk auto-approval and exception escalation; full status tracking.
- **MVP-07 Role-aware Dashboard** with the three role views and core widgets.
- **MVP-08 Audit trail** for all state changes and AI decisions.
- **MVP-09 AI guardrails:** routing (deterministic vs. LLM path), a reflection/critic check on recommendations and gap-fills, and grounding so policy claims and inferred fields trace to a source.

**MVP simplifying assumptions:**
- Travel inventory is **mocked** (static seeded dataset), not a live GDS/NDC connection.
- Card transactions and calendar entries are **simulated/seeded** to provide grounding context.
- Single tenant / single fictional company; multi-tenancy is a design consideration, not an MVP build target.
- A single travel-policy document and a small rule set, not a full policy-management product.

---

## Out of Scope

The following are explicitly **not** part of this product. Some are real capabilities of commercial T&E suites but are deliberately excluded to keep the scope realistic for one engineer.

- **OOS-01 Live travel inventory / actual booking and ticketing** (GDS, NDC, direct connects). Inventory is mocked.
- **OOS-02 Real payments, card issuance, or reimbursement disbursement.** No money moves; transactions are simulated.
- **OOS-03 Invoice / Accounts Payable** (purchase requests, three-way match, vendor payments). Invoice is a separate pillar of the T&E suites and is not part of this travel-and-expense build.
- **OOS-04 ERP / HR / accounting integrations** (for example S/4HANA, NetSuite, QuickBooks, Xero, ADP). Treated as design seams only.
- **OOS-05 Off-channel booking capture, event management, meeting-location planning.** Out of MVP focus.
- **OOS-06 A multi-agent orchestration product.** A full multi-agent report builder is an evaluation and reliability project in its own right. Atlas uses a small set of specialized agents behind one orchestrator, not a configurable agent platform.
- **OOS-07 Native mobile apps.** The product is a web app; a native mobile app is future work.
- **OOS-08 Multi-tenant SaaS administration, SSO/SAML/SCIM, billing.** Single-tenant for the build; multi-tenancy is a documented design seam.
- **OOS-09 Disruption rebooking, voice agents, deep personalization** of the kind some competitors advertise. Beyond scope.

---

## User Stories (Detailed)

Format: `As a <role>, I want <capability>, so that <outcome>.` Each story lists acceptance criteria (AC). Story IDs enable traceability into the [technical design](../design/technical-design.md), the [database schema](../design/database-schema.md) and the [traceability matrix](requirements-traceability.md).

### Employee Stories

**US-E1: Conversational trip planning**
As an employee, I want to describe a trip in plain language and receive compliant options, so that I can book quickly without learning a booking tool.
- AC1: I can enter a free-text request (e.g., "SF next Tuesday, back Thursday, under policy").
- AC2: If key details are missing (dates, destination, return), the assistant asks one focused clarifying question at a time.
- AC3: I receive a ranked shortlist of flight + hotel options, each labeled in-policy / out-of-policy with the reason.
- AC4: Each recommendation includes a short plain-language rationale (why this option) and a price/tradeoff summary.
- AC5: I can select an option, which creates a trip record in a draft/pending state.

**US-E2: Policy questions answered with sources**
As an employee, I want to ask whether something is allowed, so that I avoid out-of-policy spend.
- AC1: I can ask "can I book business class to London?" and get a yes/no/conditional answer.
- AC2: The answer cites the specific policy clause it is based on.
- AC3: If the policy is silent, the assistant says so rather than guessing.

**US-E3: Capture a receipt with minimal effort**
As an employee, I want to photograph or upload a receipt and have it become an expense entry, so that I don't type fields manually.
- AC1: I can upload an image; the system extracts amount, currency, date, vendor, location, and expense type.
- AC2: When a field is missing or ambiguous, the system infers it from my trip itinerary/calendar context and marks it as AI-inferred.
- AC3: I can review and correct any field inline before it is committed.
- AC4: The receipt is auto-attached to the resulting expense entry.

**US-E4: Auto-assembled expense report**
As an employee, I want my expense report mostly built for me, so that I only review and submit.
- AC1: Captured receipts and simulated card charges are smart-matched to the relevant trip.
- AC2: Entries are categorized and itemized automatically (e.g., hotel folio split).
- AC3: A draft report is assembled; I can review, adjust, and submit.
- AC4: Before submission, a pre-submit check flags any policy issues with explanations.

**US-E5: Track status and reimbursement**
As an employee, I want to see where my trips and reports stand, so that I know when I'll be reimbursed.
- AC1: I can see each trip/report's status (draft, submitted, approved, rejected, reimbursed-simulated).
- AC2: Rejections show the reason and what to fix.

### Manager Stories

**US-M1: Fast, contextual approvals**
As a manager, I want a queue of pending approvals with context, so that I can decide quickly and confidently.
- AC1: I see a prioritized queue of trips/reports awaiting my approval, for my direct reports only.
- AC2: Each item shows the in/out-of-policy summary, flagged exceptions, and the reason.
- AC3: I can approve, reject (with a reason), or request changes.

**US-M2: Auto-approval of low-risk items**
As a manager, I want low-risk, fully in-policy items auto-approved, so that I only spend attention on exceptions.
- AC1: Items meeting the low-risk/in-policy criteria are auto-approved and shown as such with the rationale.
- AC2: I can view (and, if needed, reverse within policy) auto-approved items.

**US-M3: Team spend visibility**
As a manager, I want to see my team's travel and expense posture, so that I can manage my budget.
- AC1: I can see aggregate team spend, pending commitments, and recent activity.
- AC2: Data is scoped to my team only.

### Finance Team Stories

**US-F1: Policy configuration**
As a finance admin, I want to define and adjust policy rules, so that enforcement matches company rules.
- AC1: I can view the active policy document and the structured rule set derived from it.
- AC2: I can express a rule in near-natural language and have it translated into a structured, testable rule (an analogue of the AI rule-authoring tools announced for commercial T&E products).
- AC3: Rules are versioned; changes are auditable.

**US-F2: Audit and exception review at scale**
As a finance admin, I want to review flagged items and the audit trail, so that I can control risk.
- AC1: I can filter expense reports/trips by exception type, status, department, and date.
- AC2: For any item I can see the full history of state changes and AI decisions (what was inferred, by which path, with what confidence).
- AC3: I can override decisions with a recorded reason.

**US-F3: Spend & automation analytics**
As a finance admin, I want a consolidated dashboard, so that I can report on spend, automation, and compliance.
- AC1: I see total/segmented spend in near-real-time across seeded data.
- AC2: I see automation rate, exception rate, and average reimbursement cycle time.
- AC3: I can view budget posture against seeded budgets.

---

## Functional Requirements

Requirement IDs (FR-x) trace to user stories and into the technical design.

### Travel Planning
- **FR-T1:** Accept free-text travel requests and parse them into structured trip intent (origin, destination, dates, travelers, constraints).
- **FR-T2:** Detect missing or ambiguous intent and prompt for clarification (one focused question per turn).
- **FR-T3:** Retrieve candidate options from the (mock) inventory matching the structured intent.
- **FR-T4:** Score and rank options against competing objectives (price, policy compliance, preference, sustainability).
- **FR-T5:** Evaluate each option against the policy engine and label in/out-of-policy with reasons.
- **FR-T6:** Produce a recommended shortlist with plain-language rationale per option.
- **FR-T7:** On selection, persist a trip record with its segments and an initial workflow state.

### Expense Creation
- **FR-X1:** Create expense entries from receipts, simulated card transactions, and trip context.
- **FR-X2:** Smart-match a transaction ↔ receipt ↔ trip line item; surface duplicates and unmatched items.
- **FR-X3:** Auto-categorize and itemize entries (e.g., split a hotel folio).
- **FR-X4:** Assemble entries into a draft expense report for a given trip or period.
- **FR-X5:** Run a pre-submit policy check and present flags with explanations before submission.

### Receipt Upload
- **FR-R1:** Accept receipt images via upload; store the image and link it to an entry.
- **FR-R2:** Run deterministic extraction and produce per-field values with confidence scores.
- **FR-R3:** Route low-confidence or missing fields to the AI gap-fill path; high-confidence fields bypass it.
- **FR-R4:** Ground inferred fields in the traveler's trip itinerary and calendar context.
- **FR-R5:** Mark each field's provenance (deterministic vs. AI-inferred) and allow inline correction.

### AI Decision Making
- **FR-A1:** A single **orchestrator** classifies each incoming task and routes it to the correct specialized agent (Travel, Expense, Policy, Approval) and the correct path (deterministic vs. LLM).
- **FR-A2:** Agents use **tool calls** to fetch ground truth (inventory, policy, profile/history, itinerary) rather than relying on model memory.
- **FR-A3:** Recommendation and gap-fill outputs pass a **reflection/critic** check (generator → critic against known facts) before being committed.
- **FR-A4:** Policy answers and inferred values include **grounding/citations** (clause reference or source field) and a confidence indicator.
- **FR-A5:** Every AI decision is logged with inputs, path taken, tools called, output, and confidence for auditability.
- **FR-A6:** The model provider is abstracted behind a provider interface so the underlying LLM can be swapped. (Public material describes Concur's receipt agent as built on Gemini models; Atlas stays model-agnostic.)

### Approval Flow
- **FR-P1:** Route a submitted trip or report to the correct approver based on the submitter's manager relationship.
- **FR-P2:** Auto-approve items meeting low-risk + fully-in-policy criteria, recording the rationale.
- **FR-P3:** Escalate exceptions to the human approver with the flagged reasons and supporting context.
- **FR-P4:** Support approve / reject-with-reason / request-changes actions.
- **FR-P5:** Track and persist every workflow state transition as an immutable audit event.

---

## Non-Functional Requirements

### Scalability
- **NFR-S1:** The system is designed as a **modular monolith** with clearly separated service boundaries (travel, expense, receipt, policy, approval, AI orchestration) so any module *could* be extracted into its own service later without redesign.
- **NFR-S2:** Stateless application layer so horizontal scaling is possible; all session and identity state is carried in tokens, not server memory.
- **NFR-S3:** Long-running AI tasks (gap-fill, ranking) are designed to run asynchronously where latency would harm UX, with the data model supporting queued/pending states.
- **NFR-S4:** Data model and access patterns are designed multi-tenant-ready (tenant scoping as a design seam) even though the build is single-tenant.

### Security
- **NFR-SEC1:** Authentication required for all non-public endpoints; short-lived access tokens.
- **NFR-SEC2:** **RBAC** enforced server-side on every request; role and permission checks are never trusted from the client.
- **NFR-SEC3:** Row/record-level scoping so users only access data they own or supervise (employee → self, manager → reports, finance → org).
- **NFR-SEC4:** Sensitive data (receipts, personal data) protected at rest and in transit; least-privilege access to stored images.
- **NFR-SEC5:** All AI prompts/responses and PII handling are logged in a way that supports audit without leaking secrets; inputs to the model are minimized to what grounding requires.
- **NFR-SEC6:** Input validation and output guardrails on all AI paths to prevent prompt-injection-driven actions (the assistant cannot take a state-changing action the user's role does not permit).

### Performance
- **NFR-P1:** Standard CRUD and dashboard reads feel instant (sub-second target) under seeded data volumes.
- **NFR-P2:** Conversational responses stream so the user sees progress rather than waiting for a full completion.
- **NFR-P3:** The deterministic path (high-confidence receipts, in-policy checks) avoids LLM calls entirely to keep cost and latency low. The routing pattern is a performance feature as much as a quality one.
- **NFR-P4:** Indexed queries for all high-frequency access patterns (see the [database schema](../design/database-schema.md)).

### Reliability
- **NFR-R1:** AI failures degrade gracefully: if the LLM path errors or times out, the system falls back to deterministic results and clearly marks fields as needing manual review rather than failing the whole flow.
- **NFR-R2:** All state transitions are transactional; partial writes never leave a report or trip in an inconsistent state.
- **NFR-R3:** Idempotent handling of receipt uploads and expense creation to avoid duplicates on retry.
- **NFR-R4:** A complete, append-only audit trail provides recoverability and traceability of every meaningful action.

---

## Success Metrics

These metrics define what "good" looks like for the product: time saved, accuracy, and automation rate.

### Time Saved
- **M-T1:** Median time from "intent to bookable recommendation" in the travel planner (target: a single short conversation rather than a multi-screen search).
- **M-T2:** Median time to produce a submittable expense report from receipts (target: review-and-submit only, minimal manual entry).
- **M-T3:** Reduction in manual fields the employee must type per expense entry.

### Expense Accuracy
- **M-A1:** Field-level extraction accuracy on receipts (deterministic + AI-inferred), measured against a labeled seed set.
- **M-A2:** Smart-match accuracy (correct transaction ↔ receipt ↔ trip linkage).
- **M-A3:** Policy-decision correctness (no false "in-policy" passes; minimal false flags) measured against a labeled evaluation set. Evaluation discipline is treated as a first-class requirement, not an afterthought.

### Automation Rate
- **M-R1:** Percentage of receipts fully resolved without manual correction.
- **M-R2:** Percentage of expense reports assembled automatically and submitted without manual line additions.
- **M-R3:** Percentage of approvals auto-approved (low-risk) vs. escalated.
- **M-R4:** Percentage of requests served by the deterministic path vs. routed to the LLM (a cost and efficiency metric that follows directly from the routing pattern).

---

*End of PRD. For what was built, how, and where it differs, see [requirements-traceability.md](requirements-traceability.md).*
