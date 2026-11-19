# Atlas RAG Masterplan

> **Archived planning document** — kept for the record of how the work was planned; the as-built system is documented in [RAG architecture](../RAG/RAG_ARCHITECTURE.md), with final numbers in [FINAL_METRICS_SUMMARY.md](../RAG/metrics/FINAL_METRICS_SUMMARY.md) and the per-task as-built logs (`R0-*` to `R6-*`) in [docs/phase_logs/](../phase_logs/README.md). Where this plan and the code disagree, the code and those as-built docs win. Several premises below were later measured to be wrong (for example the New York cosine score, see the R1 as-built log); they are kept as written. See the [archive index](README.md).

**Author:** Garv Singh Saini

**Product:** Atlas — AI-Powered Travel & Expense Copilot — RAG / Policy-Assistant Hardening
**Source of truth at planning time:** the original design documents (research notes, PRD, technical design, UI/UX, database schema, master execution plan — public edited versions now live in [docs/product](../product/PRD.md) and [docs/design](../design/technical-design.md)), **and the companion RAG research report** ([RAG_RESEARCH_REPORT.md](../RAG_RESEARCH_REPORT.md)) (*"Atlas: A Production-Grade RAG Architecture for an Enterprise T&E Policy Copilot"*) for technique rationale and math.
**Document type:** Engineering execution playbook for the *already-finished* Atlas project. This plan **upgrades the existing RAG pipeline** and **expands its knowledge base into a multi-document synthetic corpus**; it is not a redesign and adds no product features beyond the policy-assistant surface already in `PRD.md`.
**Audience:** A single engineer working through the plan one atomic task at a time, each task closed by its own validation gate.

> **How to read this plan.** Work strictly top-to-bottom. Every phase is broken into **atomic tasks** with IDs (e.g., `R2-T3`). Each task carries **Objective → Sub-steps → Validate**. A phase is "done" only when its **Definition of Done (DoD)**, **Checkpoint**, and **Phase Verification Gate** all pass. The new prefix `R{phase}-T{task}` is deliberately distinct from the original `P{n}-T{n}` tasks so the two plans never collide.

> **The cardinal rule of this plan.** Every phase runs the loop **PLAN DEEPLY → EXECUTE → VERIFY** before anything is committed. The "verify" step is the 14-gate self-verification in Section 10, run against the eval harness built in Phase R0. **Nothing merges without green gates and an evidence-backed metrics delta.** This is the whole point of the masterplan: each phase must *prove* it made retrieval more reliable or faster, not assert it.

> **Knowledge-base policy (copyright-safe).** The corpus is **100% original synthetic content** authored for this project in a single fictional universe ("Helios Dynamics"). **No third-party document text (SAP Concur templates, university policies/guides, vendor PDFs) is ever ingested, copied, or stored in the vector database.** Such documents may be consulted only as *structural* inspiration; their wording is never reused. This keeps the published portfolio free of copyright/brand risk and gives full control over evaluation ground truth.

---

# 1. RAG SYSTEM OVERVIEW (ENGINEERING PERSPECTIVE)

## The problem this plan fixes
The shipped policy assistant works as designed but is brittle at the edges. A user query *"I want to make a trip to New York"* scored **top cosine similarity 0.0726**, fell below the hand-tuned **0.20** grounding threshold, and returned *"Policy is silent on this"* — without calling the LLM. Two root causes:

1. **The knowledge base is too small.** A single hand-authored policy split into **8 clauses**. Most reasonable T&E questions have no clause to anchor on, and the assistant can answer only one *kind* of question (rules), not "how do I…" or quick-reference lookups.
2. **Retrieval and abstention are both fragile.** Dense-only cosine retrieval misses exact terms (dollar amounts, city names, clause IDs); a single global cosine threshold is query-dependent and therefore unreliable; and the confidence number is a cosmetic rescale of similarity, not a calibrated signal. A confidence of *40%* surfaced on a *silent* answer — a symptom of the brittleness and a frontend mismatch.

## The engineering thesis
Keep Atlas's existing virtues — **deterministic-first, grounded, audited, provider-abstracted** — and make retrieval *robust and fast* by (a) replacing the 8-clause stub with a realistic **multi-document synthetic corpus**, and (b) layering four proven techniques on top of the current pipeline, in priority order: **(1) the expanded corpus + an eval harness, (2) hybrid dense+sparse retrieval fused with RRF, (3) a cross-encoder reranker, (4) a calibrated, statistically-defensible abstention threshold** — then optional contextual-retrieval, query-rewriting, and generation-hardening. Every layer is independently proven and stays inside the current **FastAPI + pgvector (1536-d) + Gemini-2.5-Flash-via-OpenRouter** stack with **no schema-dimension change**.

## The multi-document corpus (the new knowledge base)
Four original Helios Dynamics documents, all internally consistent (same company, same figures), each with its own stable ID scheme so citations are unambiguous about *which* document and *which* unit answered the question. This mirrors how real enterprise assistants span several document *types* and is what makes the prototype look production-grade rather than a single-file demo.

| Document | ID scheme | Type | What it tests in RAG |
|---|---|---|---|
| **Helios Global Travel & Expense Policy** (`TEP-FY26`, ~110 clauses, ~20 sections) | `AIR-01`, `LOD-01`, `MEAL-01`, … (clause) | Authoritative rules | Rule/limit lookup; dollar/threshold/city-tier anchoring |
| **Helios T&E FAQ** (~40–60 Q&A) | `FAQ-Q01`, … (question) | Q&A | High-intent semantic matches; paraphrase robustness |
| **Helios Expense & Travel How-To Guide** (~30–40 procedures) | `HOW-01`, … (step/procedure) | Procedural | "How do I…" interface/process retrieval |
| **Helios Receipt & Documentation Quick Reference** (~20–30 entries) | `REF-01`, … (entry) | Tabular reference | Dense tabular/figure retrieval (thresholds, deadlines, affidavit rules) |

**Hard consistency requirement:** the companion documents must agree numerically with the master policy (e.g., the FAQ's "NYC per diem is $85/day" must match policy clause `MEAL-01`). Cross-document contradictions are a corpus-quality defect, caught in R0.

## Current-state component map (reconcile before editing — see R0-T1)
*As of last reconciliation; exact paths/line numbers are re-confirmed at R0-T1 because line numbers drift.*

| Concern | File (current) | Notes |
|---|---|---|
| Policy source text (8 clauses) | `database/seeds/policy_content.py` | Hand-authored; clause labels `[AIR-CABIN]`…`[SUSTAIN]`. **Replaced by the multi-document corpus.** |
| Ingestion / embedding seed | `database/seeds/seed_all.py` | Embeds each clause → 1536-d → `policy_embeddings` (pgvector, HNSW cosine). |
| Retrieval tool | `ai_agents/tools/policy_retrieval.py` | pgvector cosine top-k=5, **org-scoped via JOIN to `policies`**. |
| Grounding gate + confidence | `ai_agents/rag/retrieval.py` | Gate compares top similarity to threshold (~`:141`); confidence rescale to `[0.5, 1.0]` (~`:276`). |
| Threshold constant | `config/settings/base.py` (~`:95`) | `0.20`, calibrated against the **local lexical embedder's** score distribution. |
| Generation (cited answer) | `ai_agents/agents/policy_qa.py` | "Answer based ONLY on provided clauses; cite clause(s)"; critic self-check. |

## Target-state pipeline (end of this plan)
```
User query
  → [intent routing → Policy Agent]                         (exists)
  → [optional query rewrite / HyDE — only on weak retrieval] (R5)
  → Retrieve in parallel, org-scoped, across ALL documents:
       • Dense:  pgvector cosine top-50 (HNSW), 1536-d       (exists, widened)
       • Sparse: Postgres tsvector / ts_rank_cd top-50        (R1)
  → RRF fuse (k=60) → top-~50                                 (R1)
  → Cross-encoder rerank → top-k (5–8)                        (R2)
  → Abstention gate on CALIBRATED reranker score (τ*)         (R3)
       • below τ* → graceful abstain + escalate, NO LLM call
  → Generate (Gemini 2.5 Flash): inject parent units + IDs,
       require per-sentence citations (document + unit ID)    (exists, hardened R5)
  → Critic LLM / NLI faithfulness check → answer | needs_review (exists + R5)
  → Confidence = calibrated reranker score (not cosine rescale) (R3)
  → ai_decision_logs: query, candidates (with document_id),
       fused ranks, rerank scores, decision, citations         (R6)
```

---

# 2. TARGET ARCHITECTURE BREAKDOWN (WHERE EACH PIECE LIVES)

The **hard layering law is binding**: `services` ← `ai_agents` ← `backend` (dependencies point right-to-left; never reversed). Every new component slots into the existing planes accordingly.

- **`/services` (provider adapters, swappable, config-only):**
  - `embeddings_provider/*` — unchanged interface; may swap *model* (still 1536-d) in R4.
  - `rerank_provider/*` — **NEW** `RerankProvider` interface (R2): deterministic local reranker for CI + Cohere/BGE for production.
  - `llm_provider/*` — unchanged; reused by contextual chunking (R4), HyDE (R5), critic/NLI (R5). All behind `LLM_PROVIDER=local` for CI.
- **`/ai_agents` (the reasoning engine — most of this plan):**
  - `tools/policy_retrieval.py` — extended to hybrid + RRF (R1), to call the reranker (R2), and to retrieve across all documents while carrying `document_id` on every candidate.
  - `rag/retrieval.py` — abstention gate moves from a constant to a **calibrated τ** (R3); confidence sourced from reranker score (R3).
  - `rag/fusion.py` — **NEW** RRF implementation (R1).
  - `rag/calibration.py` — **NEW** risk-coverage / split-conformal threshold + score calibration (R3).
  - `rag/contextual.py` — **NEW** contextual-prefix generation, document-type aware (R4).
  - `rag/query_rewrite.py` — **NEW** conditional HyDE / multi-query (R5).
  - `rag/grounding.py` — citation enforcement (document + unit ID) + NLI check (R5).
  - `eval/*` — **NEW** eval harness: multi-document gold set, metrics (nDCG/recall/RAGAS), risk-coverage curve (R0).
  - `agents/policy_qa.py` — generation prompt hardened for per-sentence, document-qualified citations (R5).
- **`/database`:**
  - `seeds/corpus/*` — **NEW** one module per document (`policy.py`, `faq.py`, `howto.py`, `reference.py`) replacing the single `policy_content.py` stub (R0). All original text.
  - `seeds/seed_all.py` — seeds all four documents as rows under the demo org, then chunks + (re)embeds idempotently and populates the lexical index (R0/R1).
  - `migrations/*` — add a `tsvector` column + GIN index on the chunk/embedding table; **no change to the 1536-d vector column** (R1). If the document table lacks a `document_type`/title discriminator needed to hold multiple documents per org, add it as a **small additive column** (the only other permitted schema change — see Fence 7); anything more invasive is **flagged**, not invented. HNSW tuning is index params, not a dimension change (R6).
- **`/backend/modules/ai` (thin delegation only):** conversational endpoints unchanged in contract; they keep delegating to `/ai_agents`. Any response-shape change for citations/confidence (now document-qualified) is a **thin-layer DTO change** (in-scope per the fix-vs-flag boundary).
- **`/frontend` (experience plane):** the existing **Assistant surface** gains a proper trust surface — citation chips showing **document name + unit ID** (with hover preview), confidence from the calibrated score, and a graceful abstention + escalation CTA (R6). Semantic-only selectors; **no `data-testid` added to production components**.

---

# 3. HARD CONSTRAINTS & INVARIANTS (BINDING FENCES)

These are non-negotiable. A task that appears to require violating one must **STOP and FLAG**, not proceed.

1. **1536-d only.** The pgvector schema is 1536-dimensional. Never introduce a model whose output dimensionality differs (Gemini's 3072-d default is **incompatible**). Permitted: keep `text-embedding-3-small`; or upgrade to `text-embedding-3-large` **truncated to 1536-d and renormalized**, or Cohere `embed-v4` at **native 1536-d**. Any true dimension change is an explicit, separately-approved migration — out of scope here.
2. **Layering law.** `services` ← `ai_agents` ← `backend`. Never reversed. Rerankers/embedders live in `/services`; retrieval logic in `/ai_agents`; `backend/modules/ai` stays thin.
3. **Deterministic-first.** CI always runs on `LLM_PROVIDER=local`. **Every** new LLM- or API-touching component (reranker, contextual chunking, HyDE, NLI) MUST ship with a deterministic local mode so the eval harness and CI run with zero external calls. Live-LLM evaluation runs only in a manually-triggered `make eval-live` / `workflow_dispatch` job.
4. **Fail safe, not silent.** A degraded path (reranker unavailable, embedder error) raises-and-catches (`ProviderError`) and falls back to the prior stage marked appropriately — never a silent sentinel, never a whole-flow failure. An abstention is a *first-class, correct* outcome, not an error.
5. **Org-scoping is preserved on every retrieval path, across every document.** `policy_rules`/`policy_embeddings` carry no `organization_id`; the explicit JOIN to `policies` (the document table) MUST remain on **both** the dense and the new sparse path, and now covers all four documents uniformly. A retrieval that returns another tenant's content is a Sev-1 regression.
6. **No-double-audit.** Retrieval/abstention/answer decisions are logged once, inside the agent path, into `ai_decision_logs`. Do not add a second audit write in the backend layer.
7. **Fix-versus-flag boundary.** In scope: retrieval-tool changes, new `/ai_agents/rag/*` and `/ai_agents/eval/*` modules, thin-layer backend DTO changes, frontend wiring, the multi-document corpus, seeds, the `tsvector` migration, a small additive `document_type`/title column **only if** the document table can't already represent multiple documents per org, and index tuning. **Flag, never silently absorb:** any backend *contract* change beyond citation/confidence DTO fields, any new domain endpoint, any schema change beyond the two additive items named here, any agent-internal change that alters another agent's behavior. **Multi-document storage must reuse the existing document table additively; if it cannot, STOP and FLAG for a schema decision rather than inventing a new table.**
8. **Corpus is original and consistent.** All four documents are authored from scratch (no third-party text), and must be numerically consistent with each other. Cross-document contradictions fail the R0 gate.
9. **Threshold calibration is per-signal and per-embedder.** The legacy `0.20` was tuned to the local lexical embedder. The plan's provisional `0.30` from earlier notes is **wrong** for this distribution and would over-silence. R3 recomputes τ empirically against whatever signal (reranker score) and embedder are actually in use; if the embedder changes (R4), τ is recalibrated.
10. **Commit discipline.** Sole author; logical separation (bug fixes committed separately from feature work); **five-check secret scan on the working tree before** an explicit, narrowly-scoped `git add` (never `git add .`); known false-positive class = Pydantic field-name declarations (e.g., `RERANK_API_KEY: str = ""`). Pushes run manually from the credentialed terminal.

---

# 4. THE EXECUTION LOOP — PLAN DEEPLY → EXECUTE → VERIFY

Every task in this plan is executed in two steps — build, then document and commit — with a mandatory verification gate at the end of every phase.

## 4.1 Step 1 — Plan → Review → Execute → Report (work stays UNCOMMITTED)
Each task starts from a written task spec: context, source of truth, a reconcile-to-the-actual-codebase step, task detail, execution rules (with out-of-scope fences), the self-verification gates (Section 10) and a final status report. The approach is planned and reviewed before any code changes; only then is it implemented. The step ends with a **Final Status Report** — nothing is committed.

## 4.2 Step 2 — Document → Commit-prep (per task/phase, after gates are green)
A phase-log entry is written to `docs/phase_logs/`; a pre-drafted, sole-author commit body; a short progress note; secret-scan commands run on the working tree **before** an explicit `git add <paths>`; a `git diff --cached` review; manual push.

## 4.3 The per-phase Verification Gate (what "VERIFY at the end of every phase" means)
At the end of every phase, before the commit step, run the **14-gate self-verification** (Section 10). The gate is **evidence-backed**: it must produce a printed metrics delta from the Phase R0 eval harness (e.g., "recall@10 0.61 → 0.93; trip-to-NY now grounded; p95 +110 ms"). A phase that cannot show its intended metric move on the gold set **does not pass**, even if all code is green. This is the anti-"hallucinated-improvement" guard, the retrieval analogue of the testing phase's anti-"hallucinated-passing-tests" rule.

## 4.4 Parallel work
**Most RAG phases are sequential by data dependency** (R0 → R1 → R2 → R3). Where a phase has genuinely independent workstreams they can be built in parallel on separate branches. Authoring the four corpus documents (R0-T2) is the clearest case — the documents are independent and can be written in any order, with a consistency reconciliation pass afterward.

---

# 5. BUILD PHASES

Conventions: **Objective → Sub-steps → Validate** per task; **DoD + Checkpoint + Phase Verification Gate** per phase. Do not start a phase until the previous phase's gate is green.

---

## PHASE R0 — FOUNDATIONS: MULTI-DOCUMENT CORPUS + EVALUATION HARNESS
**Goal:** the prerequisite for everything else — an original, realistic **multi-document** policy corpus and a deterministic eval harness, so every later phase can *prove* its gains. **This phase alone resolves most of the "trip to New York" failure** by giving retrieval real, varied content to find.
**Why first:** you cannot tune retrieval you cannot measure, and the thin single-document corpus was the dominant bottleneck.

- **R0-T1 — Reconcile to actual codebase (incl. multi-document feasibility).**
  - **Objective:** confirm the real file paths, the threshold location, the embedder, the `policy_embeddings` schema, the org-scoping JOIN, **and whether the document table can hold multiple documents per org**.
  - **Sub-steps:** locate and read the six files in the Section 1 map; confirm the threshold constant and its exact line; confirm the dense SQL and the JOIN to `policies`; confirm the FK from `policy_embeddings` → `policies`; confirm the local embedder is the deterministic 1536-d one and production is `text-embedding-3-small`; **determine whether `policies` supports multiple rows per org and carries a type/title/slug discriminator** — if not, scope the *small additive* `document_type`/title column (Fence 7) or FLAG if anything more invasive is required; write a one-screen "as-built" note into `docs/phase_logs/`.
  - **Validate:** the as-built note matches reality; the multi-document storage approach is decided (reuse table additively, or flagged); any divergence from this plan's assumptions is flagged for a decision (code wins over plan).
- **R0-T2 — Author & seed the multi-document synthetic corpus.**
  - **Objective:** replace the 8-clause stub with the four original Helios Dynamics documents (Section 1 table), all internally consistent.
  - **Sub-steps:**
    - **(a) Global T&E Policy** (`TEP-FY26`, ~110 clauses across ~20 sections) — port from the companion research report's Part 4; clause IDs (`AIR-01`, `LOD-01`, `MEAL-01`, …), section headers, parent-section refs; concrete dollar caps, city tiers, day thresholds.
    - **(b) T&E FAQ** (~40–60 Q&A, IDs `FAQ-Q01…`) — high-intent employee questions whose answers restate policy rules in conversational form (e.g., "Can I expense alcohol at a team dinner?", "What's the per diem in New York?", "How long do I have to submit?"). Every answer must agree with the policy figures.
    - **(c) Expense & Travel How-To Guide** (~30–40 procedures, IDs `HOW-01…`) — step-by-step process/interface guidance (e.g., "How to submit an expense report", "How to upload a receipt", "How to request pre-trip approval", "How to add a corporate card", "How to log mileage").
    - **(d) Receipt & Documentation Quick Reference** (~20–30 entries, IDs `REF-01…`) — dense, tabular-style reference: receipt thresholds, required documentation per category, missing-receipt affidavit rules, submission deadlines, corporate-card reconciliation timing.
    - Place each as its own module under `database/seeds/corpus/`; seed each as a **row in the document table** under the demo org with `document_type` + title; idempotent seeding.
  - **Validate:** all four documents seed under the demo org; per-document unit counts match; IDs are unique within each document and namespaced by document; **a consistency check passes** (sampled figures — per diem, hotel caps, receipt thresholds, deadlines — match across policy/FAQ/reference); re-seeding is idempotent. *Parallelisable: the four documents are independent, followed by a reconciliation pass.*
- **R0-T3 — Per-document-type chunking + document-level metadata.**
  - **Objective:** chunk each document by its natural unit and stamp document-level metadata so retrieval and citations are document-aware.
  - **Sub-steps:** chunk the **policy** by clause, the **FAQ** by Q&A pair, the **how-to** by step/procedure, the **reference** by entry/row; stamp every chunk with `document_id`, `document_title`, `document_type`, the within-document unit ID (`clause_ref`/`question_ref`/`step_ref`/`entry_ref`), `section`, and `parent_section_id`; adopt **parent-document retrieval** per document type (embed the small unit, return the enclosing section/answer/procedure to the generator).
  - **Validate:** each chunk maps to exactly one unit and carries full document metadata; parent lookup returns the correct enclosing context per document type.
- **R0-T4 — Re-embed corpus (1536-d, idempotent, all documents).**
  - **Objective:** embed every chunk across all four documents with the current 1536-d embedder and store in `policy_embeddings`.
  - **Sub-steps:** wire chunking → embeddings provider → `policy_embeddings` with the unit ID + document metadata; make re-indexing idempotent (re-run yields identical counts, no duplicates).
  - **Validate:** total embedding count == total chunk count, and the per-document breakdown matches; HNSW cosine index present; re-running is a no-op.
- **R0-T5 — Build the labeled gold eval set (multi-document).**
  - **Objective:** a versioned dataset of queries with expected behavior, spanning all document types, including cross-document and genuinely out-of-scope queries.
  - **Sub-steps:** create ~120–160 **answerable** queries, each tagged with the **document + within-document unit ID(s)** that answer it; cover **each document type** (rule lookups → policy, conversational → FAQ, "how do I…" → how-to, threshold/deadline → reference); include **cross-document** queries whose best answer needs more than one document (e.g., a rule from the policy *and* the procedure from the how-to); include the literal *"I want to make a trip to New York"* query, expecting grounded retrieval of `PTA-01/02`, `AIR-01/03`, `LOD-01` (NYC Tier-1 $300/night), `MEAL-01`, and plausibly a how-to booking procedure; add ~40–60 **unanswerable / out-of-scope** queries that MUST abstain; store as a fixture under `ai_agents/eval/`.
  - **Validate:** dataset loads; every answerable item references real (document, unit) pairs; cross-document items reference ≥2 documents; unanswerable items are clearly outside the whole corpus.
- **R0-T6 — Stand up the eval harness (deterministic-first, CI-gated).**
  - **Objective:** compute retrieval and generation metrics reproducibly with no external calls, with an optional per-document-type breakdown.
  - **Sub-steps:** implement **retrieval metrics** (Recall@k, Precision@k, MRR, nDCG@10) and **generation metrics** via RAGAS-style faithfulness / context-precision using the **local** provider; support an optional metric breakdown by `document_type`; implement a **risk-coverage curve** + AURC over an abstention signal; add a `make eval` target on `LLM_PROVIDER=local`; add a separate `make eval-live` (`workflow_dispatch`) for Gemini; record a **baseline** snapshot of the *current* pipeline for later deltas.
  - **Validate:** `make eval` runs deterministically and prints the metric table (overall + per-document-type) + baseline; CI executes it green; `make eval-live` is gated/manual.

**DoD:** four-document original corpus (consistent) seeded + chunked with document metadata + embedded (1536-d, idempotent); multi-document gold set (per-type + cross-document + unanswerable) in place; deterministic eval harness printing nDCG@10 / recall@k / faithfulness / risk-coverage; a recorded baseline.
**Checkpoint:** a fresh clone can seed all four documents, embed, and run `make eval` to a stable baseline number.
**Phase Verification Gate:** Section 10 gates 1–11 (terminal) green; baseline metrics captured; **trip-to-NY now retrieves grounded clauses even before hybrid/rerank** (corpus effect) — record the before/after; cross-document consistency check passes.

---

## PHASE R1 — HYBRID RETRIEVAL: DENSE + SPARSE + RRF
**Goal:** fix recall on exact terms and lexically-distant queries by adding sparse (BM25-flavored) retrieval alongside dense, fused with Reciprocal Rank Fusion, across all documents. Largest reliability gain for the least latency, zero new infrastructure.
**Math anchor:** `RRF(d) = Σ_{r∈{dense,sparse}} 1/(k + rank_r(d))`, `k=60`.

- **R1-T1 — Lexical index over all chunks.**
  - **Objective:** Postgres full-text search over every document's chunk text.
  - **Sub-steps:** add a `tsvector` column (generated from chunk text + unit ID + section title + document title) and a **GIN index** via an **additive** Alembic migration; **no change to the 1536-d vector column**; populate during seeding for all four documents (extend `seed_all.py`).
  - **Validate:** migration applies and downgrades cleanly; `to_tsquery`/`plainto_tsquery` returns expected units for exact terms ("$300", "New York", "AIR-03", "affidavit") across documents.
- **R1-T2 — Parallel dense + sparse retrieval (org-scoped, multi-document).**
  - **Objective:** retrieve top-50 from each path, org-scoped, spanning all documents.
  - **Sub-steps:** in `policy_retrieval.py`, run the existing dense cosine query (widened to top-50) and a new `ts_rank_cd` lexical query (top-50) **both** with the JOIN to the document table for org-scoping; carry `document_id` + unit ID on every candidate; return both ranked lists.
  - **Validate:** both lists are org-scoped (cross-tenant probe returns nothing foreign); each returns ≤50 candidates; candidates carry document metadata.
- **R1-T3 — RRF fusion.**
  - **Objective:** combine the two lists without score normalization.
  - **Sub-steps:** implement `rag/fusion.py` with `reciprocal_rank_fusion(lists, k=60)`; unit-test the formula against a hand-worked example (known ranks → known fused order); make `k` a config value.
  - **Validate:** unit test matches hand-computed fused ranks; ties handled deterministically.
- **R1-T4 — Wire fused retrieval behind the existing tool interface.**
  - **Objective:** the Policy Agent calls the same tool; internals are now hybrid + multi-document.
  - **Sub-steps:** `policy_retrieval.py` returns the RRF-fused top-N (N≈50) with metadata intact (document, unit ID, parent context); the tool's public signature is unchanged (thin-layer compatibility).
  - **Validate:** the agent path runs unchanged; fused output carries document + unit + parent refs.
- **R1-T5 — Re-run eval; assert recall gain + NY regression.**
  - **Objective:** prove hybrid beats dense-only.
  - **Sub-steps:** run `make eval`; compare recall@10 / nDCG@10 to the R0 baseline (overall and per document type); assert the *"trip to New York"* query surfaces the expected units in the fused top-k; capture the latency delta (~negligible, parallel).
  - **Validate:** recall@10 improves materially over baseline; NY regression test passes; latency within budget.

**DoD:** hybrid dense+sparse retrieval fused with RRF, org-scoped on both paths, spanning all documents, behind the unchanged tool interface; recall improved over baseline with evidence.
**Checkpoint:** exact-term and lexically-distant queries retrieve the right unit from the right document; NY is no longer silent for lack of recall.
**Phase Verification Gate:** Section 10 gates 1–11 green; printed recall@10/nDCG delta vs baseline; org-scoping probe clean on the new sparse path; idempotent re-index holds.
*Parallelisable:* R1-T1 (migration/index) and R1-T3 (fusion module + unit tests) are independent; R1-T4/T5 are sequential.

---

## PHASE R2 — PRECISION: CROSS-ENCODER RERANKING
**Goal:** add a two-stage **retrieve-then-rerank** step so the top-k handed to the generator is high-precision. Corrects near-misses cosine and BM25 both rank too high — including cross-document near-misses (a how-to step vs a policy clause vs an FAQ answer).
**Intuition:** a bi-encoder scores `cos(emb(q), emb(d))` with query and doc encoded *independently*; a cross-encoder scores `s(q,d) = CrossEncoder([q ; d])` with full token-level attention between them — far more accurate, at the cost of one forward pass per candidate (hence only over the top-50, not the whole corpus).

- **R2-T1 — `RerankProvider` interface in `/services` (deterministic-first).**
  - **Objective:** a swappable reranker with a CI-safe local mode.
  - **Sub-steps:** define `RerankProvider.rerank(query, candidates) -> [(idx, score)]`; implement a **deterministic local reranker** (lexical-overlap / feature-based) for `LLM_PROVIDER=local`/CI and a **production** adapter (Cohere Rerank or hosted `bge-reranker-v2-m3`) selected by config; respect the layering law (lives in `/services`).
  - **Validate:** both modes satisfy the interface; CI uses the local reranker with no external call; a smoke test exercises the interface.
- **R2-T2 — Two-stage pipeline.**
  - **Objective:** rerank the fused top-50 down to top-k (5–8).
  - **Sub-steps:** pass the RRF-fused top-50 (with document metadata) to `RerankProvider`; take the top-k by reranker score; cap candidate count and batch the call; on reranker failure, **fall back to the RRF order** marked accordingly (fail-safe).
  - **Validate:** reranked top-k differs from (and improves on) the pre-rerank order on fixtures; induced reranker failure degrades to RRF order, never errors the flow.
- **R2-T3 — Score calibration.**
  - **Objective:** make reranker scores comparable across queries (threshold-able in R3).
  - **Sub-steps:** fit **Platt scaling** (`P = 1/(1+exp(A·s + B))`) or isotonic regression on a held-out slice of the gold set so `0.7 ≈ 70%` relevance; store calibration params; expose a calibrated score per result.
  - **Validate:** calibrated scores are monotonic in true relevance on the held-out slice; the same calibrated value means the same thing across queries and document types.
- **R2-T4 — Re-run eval; assert precision gain + latency budget.**
  - **Objective:** prove rerank improves ordering within an acceptable latency cost.
  - **Sub-steps:** run `make eval`; compare nDCG@5 / Precision@k to R1; measure added latency (target ~+80–200 ms p50 production; local CI ≈0); confirm Section 6 budget.
  - **Validate:** nDCG@5 improves over R1; p95 within budget; CI green on the local reranker.

**DoD:** two-stage retrieve→rerank with a swappable, CI-safe `RerankProvider`; calibrated scores; precision improved over R1 with evidence; latency within budget.
**Checkpoint:** near-miss queries (e.g., "alcohol at dinner" → entertainment clause, not per-diem; "how do I submit" → how-to, not policy) are correctly ordered.
**Phase Verification Gate:** Section 10 gates 1–11 green; printed nDCG@5 + latency delta; fail-safe fallback verified.

---

## PHASE R3 — TRUSTWORTHY ABSTENTION: CALIBRATED THRESHOLD
**Goal:** replace the brittle `0.20` cosine gate with a **statistically-defensible** answer/abstain decision on the **calibrated reranker score**, and fix the confidence/"why this answer" mismatch. This is the phase that makes the assistant *trustworthy* and *defensible to a reviewer*.
**Math anchor:** answer iff top calibrated score `> τ*`, where `τ* = min{τ : Risk(τ) ≤ r*}` with `Risk(τ) = Σ_i (1−Acc_i)·1[score_i>τ] / Σ_i 1[score_i>τ]` (target risk `r*`, e.g. 0.05); equivalently the split-conformal quantile `τ = S_(⌈(1−α)(n+1)⌉)` for a `1−α` coverage guarantee.

- **R3-T1 — Threshold computation over the calibration set.**
  - **Objective:** compute τ* from data, not by hand.
  - **Sub-steps:** implement `rag/calibration.py` to sweep τ over the gold set, plot the **risk-coverage curve**, compute **AURC**, and select τ* for a target precision floor on answered queries (e.g., ≥95%) — risk-coverage (SGR) or split-conformal; persist τ* and the curve under `ai_agents/eval/`.
  - **Validate:** τ* is reproducible; the risk-coverage curve and AURC print; AURC for the reranker signal beats the cosine baseline.
- **R3-T2 — Swap the gate.**
  - **Objective:** retire the constant.
  - **Sub-steps:** in `rag/retrieval.py`, replace the `0.20`-against-cosine gate with `calibrated_reranker_score > τ*`; remove/deprecate the `0.20` constant in `config/settings/base.py` (keep a config knob for *target risk*, not a magic number); ready a recalibration hook for R4's possible embedder change.
  - **Validate:** the gate keys off the calibrated reranker score; the legacy constant no longer drives behavior; out-of-scope gold queries abstain, answerable ones pass.
- **R3-T3 — Fix the confidence / "why this answer" mismatch.**
  - **Objective:** eliminate the bug where a *silent* answer showed *40%* confidence and "Resolved with LLM reasoning."
  - **Sub-steps:** ensure the **silent branch** sets `confidence = None`, performs **no LLM call**, and returns a *silent-specific* rationale; ensure the **answered branch** sources confidence from the **calibrated reranker score** (not the `[0.5,1.0]` cosine rescale); thread the correct rationale + document-qualified citations through the thin backend DTO to the frontend (in-scope DTO change).
  - **Validate:** a forced-silent query returns `confidence=None`, no LLM invocation (assert via logs), and the silent rationale; an answered query returns a calibrated confidence and a grounded, document-qualified rationale.
- **R3-T4 — Production-grade abstention + escalation UX (logic).**
  - **Objective:** the requested behavior — "not in the company's documents, contact your team lead."
  - **Sub-steps:** implement a blameless abstention payload: a clear "not covered by the Helios T&E documents" message, an **escalation target** (team lead / Travel Desk contact from the policy's `CONT-01/02`), and, when available, the **nearest in-scope** suggestion (which may point to any document — "the documents don't cover X, but the FAQ covers Y — want that?"); never fabricate, never answer policy from world knowledge.
  - **Validate:** out-of-scope queries return the abstention payload with an escalation target; no world-knowledge policy answers leak; nearest-topic suggestion appears (cross-document allowed) when a near unit exists.
- **R3-T5 — Re-run eval; assert the abstention contract.**
  - **Objective:** prove the answer/abstain tradeoff.
  - **Sub-steps:** run `make eval`; assert **precision ≥ target** on answered queries and **recall = 1.0** (every unanswerable gold query abstains); report coverage at τ*; compare AURC to the cosine baseline.
  - **Validate:** precision floor met on answered; zero unanswerable queries answered; coverage acceptable; AURC improved.

**DoD:** abstention driven by a calibrated reranker score at a data-derived τ*; confidence/rationale bug fixed; blameless cross-document escalation behavior; precision floor + zero-false-answer on out-of-scope, with evidence.
**Checkpoint:** the assistant answers confidently when grounded (citing the right document) and says "not in the documents — contact your team lead" cleanly when not, with no spurious confidence number.
**Phase Verification Gate:** Section 10 gates 1–11 green; printed risk-coverage curve + AURC vs baseline + coverage/precision/abstain-recall; **no-LLM-on-silent** asserted from logs.

---

## PHASE R4 — CONTEXTUAL RETRIEVAL & (OPTIONAL) EMBEDDING UPGRADE
**Goal:** a further recall bump that stays schema-compatible — Anthropic-style **Contextual Retrieval** (a short context prefix per unit before embedding/indexing) and, optionally, a 1536-d embedding-model upgrade.

- **R4-T1 — Contextual prefixes (deterministic-first, document-aware).**
  - **Objective:** prepend a 50–100-token situating sentence to each unit before embedding and lexical indexing, **naming its document and section** ("This is from the Helios T&E FAQ; it answers whether alcohol is reimbursable at team dinners." / "This is from the Air Travel section of the Helios Global T&E Policy; it sets cabin-class eligibility by flight duration.").
  - **Sub-steps:** implement `rag/contextual.py` to generate prefixes via the LLM provider, **prompt-cached**, with a **deterministic template fallback** for CI; apply to both the embedding text and the `tsvector` text, per document type.
  - **Validate:** every chunk has a document-aware contextual prefix; CI path uses the deterministic template (no external call); prefixes are stored/traceable.
- **R4-T2 — Re-embed with prefixes (idempotent).**
  - **Objective:** refresh `policy_embeddings` and the lexical index with contextualized text across all documents.
  - **Sub-steps:** re-run the idempotent indexer; verify per-document counts; keep the 1536-d schema.
  - **Validate:** counts stable; re-run is a no-op; dimensionality unchanged.
- **R4-T3 — (Optional) embedding-model upgrade, config-only.**
  - **Objective:** a free quality bump within 1536-d if the eval justifies it.
  - **Sub-steps:** behind config, swap to `text-embedding-3-large` **truncated to 1536-d + renormalized**, or Cohere `embed-v4` at **native 1536-d**; re-embed the corpus with the *same* model used for queries; **recalibrate τ\*** (R3) for the new score distribution.
  - **Validate:** same 1536-d schema; query/doc embedder match; τ* recalibrated; eval shows a net gain (else revert — keep `text-embedding-3-small`).
- **R4-T4 — Re-run eval; assert failure-rate reduction.**
  - **Objective:** confirm contextualization (and any model swap) helped.
  - **Sub-steps:** run `make eval`; compare retrieval-failure-rate / nDCG to R3; keep changes only if they improve the gold-set numbers.
  - **Validate:** retrieval-failure-rate drops vs R3 (or the change is reverted with a recorded reason).

**DoD:** document-aware contextual prefixes across embedding + lexical paths (deterministic-first); optional 1536-d model upgrade adopted only if eval-positive; τ* recalibrated if the embedder changed.
**Checkpoint:** ambiguous, context-dependent queries retrieve better and land in the right document; schema and dimensionality unchanged.
**Phase Verification Gate:** Section 10 gates 1–11 green; printed failure-rate/nDCG delta vs R3; dimensionality invariant confirmed; τ* recalibration evidenced if applicable.

---

## PHASE R5 — QUERY UNDERSTANDING & GENERATION HARDENING
**Goal:** close the remaining lexical-gap cases and tighten hallucination control on the generation side, without paying latency on every query.

- **R5-T1 — Conditional query rewrite / HyDE (latency-gated).**
  - **Objective:** bridge query↔document space only when first-pass retrieval is weak.
  - **Sub-steps:** implement `rag/query_rewrite.py`; fire HyDE (LLM generates a hypothetical answer doc → embed that) or multi-query expansion **only** when the fused/calibrated top score falls in a "weak" band — not on every query; deterministic stub for CI.
  - **Validate:** rewrite triggers only on weak retrieval (assert via logs); previously-weak gold queries improve; strong queries skip the extra LLM call (latency unaffected).
- **R5-T2 — Citation enforcement (document-qualified).**
  - **Objective:** every answer sentence must cite a **document + unit ID**.
  - **Sub-steps:** harden the `policy_qa.py` prompt for per-sentence citations naming the source document and unit (e.g., "T&E Policy §LOD-01", "T&E FAQ Q-07", "How-To HOW-03"); post-process to **drop or flag** any sentence lacking a supporting citation; surface citations as structured data (document, unit, snippet) for the frontend chips.
  - **Validate:** answered responses carry per-sentence document-qualified citations; an uncited sentence is removed/flagged; citations are machine-readable with document identity.
- **R5-T3 — NLI grounding check (no-double-audit).**
  - **Objective:** a deterministic-leaning supplement to the existing critic LLM.
  - **Sub-steps:** add an NLI/entailment check that the answer is entailed by the retrieved units; on failure return `needs_review`/abstain; **reuse the single audit write** (do not add a second).
  - **Validate:** an answer not entailed by context is caught; the decision is logged once; no double-audit.
- **R5-T4 — Re-run eval; assert faithfulness gain.**
  - **Objective:** prove tighter grounding.
  - **Sub-steps:** run `make eval`; compare RAGAS faithfulness / context-precision to R4; confirm latency budget (HyDE only on weak queries).
  - **Validate:** faithfulness improves; no latency regression on the common path.

**DoD:** conditional HyDE/rewrite on weak retrieval only; per-sentence document-qualified citation enforcement; NLI grounding check sharing the single audit write; faithfulness improved with no common-path latency cost.
**Checkpoint:** lexically-distant phrasings still get grounded answers; every answer is cited to a specific document + unit and entailment-checked.
**Phase Verification Gate:** Section 10 gates 1–11 green; printed faithfulness/context-precision delta; HyDE-trigger-rate and latency evidence.

---

## PHASE R6 — PRODUCTION HARDENING, TRUST SURFACE & FINAL VERIFICATION
**Goal:** make it fast, make the trust visible in the UI, complete the audit trail, and produce the public write-up.

- **R6-T1 — Latency hardening.**
  - **Objective:** hit the speed goal.
  - **Sub-steps:** tune pgvector HNSW (`m=16`, `ef_construction=128–200`, raise `hnsw.ef_search` to trade latency for recall), consider `halfvec` storage; prompt-cache the system prompt + retrieved context; profile end-to-end against the Section 6 budget; build indexes with `CREATE INDEX CONCURRENTLY`.
  - **Validate:** p50/p95 within budget; recall held at the higher `ef_search`; no index-build downtime.
- **R6-T2 — Frontend trust surface (multi-document citations).**
  - **Objective:** make grounding visible (the enterprise-copilot pattern).
  - **Sub-steps:** in the existing Assistant surface, render **citation chips showing document name + unit ID** with a hover preview of the cited snippet, **confidence** from the calibrated score, and the **graceful abstention + escalation CTA**; semantic-only selectors; **no `data-testid`** on production components.
  - **Validate:** answered responses show document-qualified citation chips + calibrated confidence; abstentions show the escalation CTA; chips preview the cited unit from the correct document.
- **R6-T3 — Audit completeness.**
  - **Objective:** the new pipeline is fully traceable.
  - **Sub-steps:** ensure `ai_decision_logs` records query, candidate set (each with `document_id` + unit ID), fused ranks, reranker scores, the calibrated top score, the answer/abstain decision, and the document-qualified citations — **once**, inside the agent path.
  - **Validate:** a sampled query yields a complete single decision-log row covering every new stage, including which document(s) the answer drew from.
- **R6-T4 — Full regression eval in CI.**
  - **Objective:** lock in the gains.
  - **Sub-steps:** run the entire multi-document gold set through `make eval` in CI on `LLM_PROVIDER=local`; emit the final metrics table (overall + per-document-type) + risk-coverage curve as build artifacts; set regression thresholds so a future change that drops recall/precision/faithfulness fails CI.
  - **Validate:** CI green; regression thresholds enforced; artifacts emitted.
- **R6-T5 — Public documentation.**
  - **Objective:** the presentable writeup.
  - **Sub-steps:** write `docs/RAG_ARCHITECTURE.md` summarizing the pipeline, the **multi-document corpus** (the four documents + ID schemes), the **RRF formula**, the **calibrated-abstention formulation** (risk-coverage + conformal), the before/after metrics (baseline → final, overall + per type), and a pointer to the companion research report; include the comparison table and the "why this is the best choice" justification.
  - **Validate:** the doc builds; the math and metric deltas are correct and self-consistent with the eval artifacts.

**DoD:** latency within budget; visible multi-document trust surface (document-qualified citations, confidence, escalation); complete single-write audit; CI-enforced regression gates; publication-ready architecture doc with real before/after numbers.
**Checkpoint:** the finished assistant is fast, grounded across multiple documents, cited to specific units, gracefully abstaining, fully audited, and documented end-to-end.
**Phase Verification Gate:** **all 14** Section 10 gates green (terminal + manual tiers); final metrics report shows the cumulative delta from the R0 baseline; trip-to-NY answered with document-qualified citations + calibrated confidence.

---

# 6. LATENCY BUDGET (TARGET)

| Stage | Budget (p50) | Notes |
|---|---|---|
| Dense + sparse retrieve (parallel, all docs) | ~negligible | Small corpus; exact scan is sub-ms, HNSW for headroom. |
| RRF fusion | ~0 | O(candidates) in app code. |
| Cross-encoder rerank (top-50) | +80–200 ms | Cap candidates at 50; batch; local CI reranker ≈0. |
| Calibrated abstention | ~0 | Single comparison. |
| Query rewrite / HyDE | +1 LLM call | **Only** on weak retrieval; gated. |
| Generation (Gemini 2.5 Flash) | model-bound | Prompt-cache system + retrieved context. |
| Critic / NLI check | +1 call (light) | Reuse single audit write. |

**Rule:** the reranker is the main added cost and is worth ~100 ms to avoid a missed/hallucinated unit; HyDE is never on the common path; prompt caching offsets the contextual/system overhead.

---

# 7. DEPENDENCY MAP

**Phase order (hard):** `R0` (multi-document corpus + eval) → `R1` (hybrid + RRF) → `R2` (rerank) → `R3` (calibrated abstention) → `R4` (contextual + optional embed upgrade) → `R5` (query/gen hardening) → `R6` (hardening + UI + docs).

**Why the order is fixed:**
- **R0 gates everything** — no later phase can prove a gain without the eval harness and a real, varied corpus.
- **R2 needs R1** — the reranker rescores the fused candidate set.
- **R3 needs R2** — the abstention signal is the *calibrated reranker score*; calibrating a weaker signal first would waste work.
- **R4 may re-trigger R3** — changing the embedder changes the score distribution → recalibrate τ*.
- **R5 sits on top** — query rewrite and generation hardening assume a working retrieve→rerank→gate core.
- **R6 is last** — UI/latency/docs lock in a verified pipeline.

**Module dependencies:** `/services` (embeddings, **rerank**) ← `/ai_agents` (`tools/policy_retrieval`, `rag/fusion|calibration|contextual|query_rewrite|grounding`, `eval/*`, `agents/policy_qa`) ← `backend/modules/ai` (thin) ← `/frontend` (Assistant trust surface). Org-scoping JOIN to the document table on every retrieval path, across all documents. Single audit write in `ai_decision_logs`.

**Safe parallelism:** within R0, the four corpus documents (R0-T2) are independent — author each separately, then run a consistency reconciliation pass; dataset authoring (R0-T5) and harness scaffolding (R0-T6) can also split. Within R1, the lexical-index migration (R1-T1) and the fusion module (R1-T3) can split. Everything touching `rag/retrieval.py`'s gate (R3) stays single-threaded.

---

# 8. RISK MANAGEMENT (RAG-SPECIFIC)

- **Hallucinated improvement.** *Risk:* a phase "feels" better but didn't move the gold-set metric. *Mitigation:* the Phase Verification Gate demands a printed metric delta vs the R0 baseline; no delta → no pass. *Rule:* every phase proves its number.
- **Cross-document inconsistency.** *Risk:* the FAQ/reference contradict the policy (e.g., different per diem), so retrieval returns conflicting answers. *Mitigation:* the R0-T2 consistency check on sampled figures; a corpus contradiction fails the R0 gate. *Rule:* one source of truth for every number, restated consistently across documents.
- **Over-silencing / under-silencing.** *Risk:* a mis-set threshold abstains on answerable queries or answers out-of-scope ones. *Mitigation:* data-derived τ* with a precision floor and recall=1.0 on the unanswerable set; risk-coverage/AURC reported each time. *Rule:* never hand-pick a threshold; recalibrate when the signal or embedder changes.
- **Dimensionality drift.** *Risk:* an embedder swap breaks the 1536-d schema. *Mitigation:* the 1536-d fence; truncate+renormalize or native-1536 only. *Rule:* any real dimension change is a separate, approved migration.
- **Multi-document schema overreach.** *Risk:* storing multiple documents tempts a new table or invasive migration. *Mitigation:* reuse the existing document table additively (small `document_type`/title column at most); R0-T1 decides this explicitly. *Rule:* anything beyond the two named additive items → STOP and FLAG.
- **Cross-tenant leak on the new sparse path.** *Risk:* the lexical query forgets the JOIN to the document table. *Mitigation:* org-scoping asserted on both paths in R1-T2 and in the gate's adversarial pass. *Rule:* a foreign-tenant unit in results is Sev-1.
- **Provider latency/availability (reranker, embedder, LLM).** *Risk:* an external call spikes or fails. *Mitigation:* fail-safe fallbacks (rerank→RRF order; deterministic local modes for CI); latency budget with p95 guard. *Rule:* every external call degrades gracefully, never errors the flow.
- **Confidence/UX regressions.** *Risk:* the cosmetic-confidence bug recurs or citations go stale. *Mitigation:* R3-T3 invariants (silent ⇒ confidence=None, no LLM) and R5-T2 citation enforcement are gate items. *Rule:* silent answers carry no confidence and make no LLM call.
- **Copyright/brand creep.** *Risk:* someone ingests a real SAP/university PDF "just to bulk up the corpus." *Mitigation:* Fence 8 — corpus is original synthetic only. *Rule:* third-party text is never stored in the vector DB; consult only for structure.
- **Frontier over-engineering.** *Risk:* reaching for GraphRAG/ColBERT/RAPTOR/full Self-RAG on a focused corpus. *Mitigation:* this plan defers them; borrow only the *concepts* (CRAG evaluator, Self-RAG critique). *Rule:* don't add frontier complexity until corpus growth justifies it — STOP and FLAG.
- **Scope drift.** *Risk:* new endpoints/features creep in. *Mitigation:* the fix-vs-flag boundary; out-of-scope fences. *Rule:* if it's not retrieval/eval/thin-DTO/frontend-wiring/corpus/index, flag it.

---

# 9. DEVELOPMENT RULES

1. **Plan deeply, then execute, then verify — every phase.** Written plan → review → implementation → Final Status Report (uncommitted) → 14-gate verification → documentation and commit.
2. **One atomic `R*-T*` at a time.** Implement, run its Validate line, then stop. Never batch blindly.
3. **Prove the metric.** No phase is "done" until it shows its intended gold-set delta from the R0 baseline. Hallucinated improvements are blocking failures.
4. **Reconcile to the actual codebase.** Code wins over this plan; re-confirm paths/line numbers/SQL/schema before editing; flag divergences for a decision rather than silently absorbing them.
5. **Honor the layering law.** `services` ← `ai_agents` ← `backend`. Rerankers/embedders in `/services`; retrieval in `/ai_agents`; backend stays thin.
6. **Deterministic-first, always.** Every LLM/API-touching component ships a local mode; CI runs `LLM_PROVIDER=local`; live-LLM eval is manual.
7. **1536-d only.** No dimensionality change. Truncate+renormalize or native-1536 only.
8. **Corpus is original and consistent.** Author all four Helios documents from scratch; never ingest third-party text; keep figures consistent across documents.
9. **Preserve org-scoping on every retrieval path** (JOIN to the document table), across all documents, including the new sparse path.
10. **Fail safe, never silent.** Graceful degradation via raise-and-catch; abstention is a correct outcome; no whole-flow failures.
11. **One audit write.** Log retrieval/abstention/answer decisions once, in the agent path, into `ai_decision_logs`, including `document_id` on candidates.
12. **Recalibrate, don't hardcode.** τ* comes from the calibration set; the only knob is the target risk, never a magic number; recalibrate when the signal/embedder changes.
13. **Multi-document storage reuses the existing table additively.** Small `document_type`/title column at most; anything more invasive → STOP and FLAG.
14. **Commit discipline + no `data-testid`.** Sole author; bug fixes separated from feature work; five-check secret scan before an explicit narrow `git add` (never `git add .`); known false-positive = Pydantic field-name declarations; push manually; frontend tests use semantic selectors only.

---

# 10. THE 14-GATE SELF-VERIFICATION TEMPLATE

Run at the end of every phase (subset applies per phase; R6 must pass all 14). **Tier 1 is terminal-provable; Tier 2 is manual/observed.** Each gate that involves a metric must print evidence (the number and its delta from baseline).

**Tier 1 — terminal-provable**
1. **Lint clean** — `ruff` passes on touched backend/config.
2. **Types clean** — `mypy` passes on backend/config (note: `ai_agents/`, `services/` excluded from the mypy target — verified locally only, consistent with existing CI scope).
3. **Unit tests pass** — fusion/rerank/calibration/retrieval unit tests green (incl. the hand-worked RRF case).
4. **Eval runs deterministically** — `make eval` completes on `LLM_PROVIDER=local` with no external calls.
5. **Retrieval metric meets phase threshold** — recall@10 / nDCG@10 (or nDCG@5 for R2) improves vs the prior phase / baseline; number printed (overall + per-document-type where relevant).
6. **Abstention metric meets phase threshold** *(R3+)* — precision floor on answered (e.g., ≥95%) and recall = 1.0 on the unanswerable gold set; coverage + AURC printed.
7. **Trip-to-NY regression passes** — the query retrieves the expected units and (post-R3) is answered with document-qualified citations, not silent.
8. **Latency within budget** — p50/p95 measured against Section 6; printed.
9. **Idempotent re-index** — re-running seed/embedding yields identical counts (overall + per document), no duplicates.
10. **Secret scan clean** — five-check scan on the working tree returns no hits (Pydantic field-name false positives excluded).
11. **Migrations apply + downgrade cleanly** *(when schema touched)* — the additive `tsvector`/GIN (and any additive `document_type`) migration up/down is clean; 1536-d vector column unchanged.

**Tier 2 — manual / observed**
12. **"Try to break it" adversarial pass** — paraphrases, out-of-scope queries, exact-figure queries ("$300 in NYC?"), cross-document queries ("what's the rule *and* how do I file it?"), and a prompt-injection-in-query probe all behave correctly (answer-when-grounded, abstain-when-not, no injection compliance).
13. **Frontend trust surface** *(when FE touched)* — document-qualified citation chips (document name + unit ID + hover preview), calibrated confidence, and the abstention/escalation CTA render correctly; semantic selectors only.
14. **Audit completeness** — a sampled query produces a single complete `ai_decision_logs` row (query, candidates with `document_id`, fused ranks, reranker scores, calibrated top score, decision, document-qualified citations).

---

# 11. OUT-OF-SCOPE FENCES (BINDING)

- **No dimensionality change** to the vector schema (1536-d only).
- **No third-party document text** in the corpus or vector DB — original synthetic content only; real docs consulted for structure, never copied.
- **No new domain endpoints** or backend contract changes beyond citation/confidence DTO fields.
- **No schema changes** other than the additive lexical column + GIN index and (only if needed) a small additive `document_type`/title column on the existing document table.
- **No new tables** for multi-document storage — reuse the existing document table; FLAG if impossible.
- **No agent-internal changes** that alter another agent's behavior (Policy Agent retrieval internals only).
- **No frontier methods** (GraphRAG, ColBERT/ColPali, RAPTOR, full Self-RAG, agentic multi-hop loops) — deferred until corpus growth justifies them; STOP and FLAG.
- **No second audit write**, no `git add .`, no automated push.
- **No live-LLM dependency in CI** — deterministic-first is mandatory.

---

*This document is the full RAG-hardening playbook for Atlas. Execute phases R0→R6 in order; plan deeply, execute atomically, and verify against the eval harness at every phase gate. The corpus is an original, internally-consistent four-document set authored for this project. The companion research report holds the technique rationale and full mathematical formulations. Code wins over this plan — reconcile, and flag divergences rather than deviating.*

*End of Atlas RAG Masterplan.*
