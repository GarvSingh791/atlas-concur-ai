# Atlas documentation

This is the map of every document in the Atlas repository, with one line on what each is for. Start with a reading path below if you have limited time, or jump to a section. The [top-level README](../README.md) has the project summary, the live demo and the headline results.

Documents are grouped the [Diátaxis](https://diataxis.fr/) way: **explanation** (why and how the system is built), **how-to guides** (do a task), **reference** (look a fact up), plus the product docs, the quality evidence and the build history.

---

## Start here

| If you have | Read, in this order |
|---|---|
| **2 minutes** (what is this, and is it real?) | [README](../README.md) at a glance and GIF → [concept map](product/concur-to-atlas-map.md) "At a glance" table → [TESTING.md](quality/TESTING.md) summary table |
| **20 minutes** (how is it built, and how do I know it works?) | [ARCHITECTURE.md](ARCHITECTURE.md) → [ADRs 0001–0008](adr/README.md) → [security model](design/security-model.md) → [TESTING.md](quality/TESTING.md) → one story from the [phase logs](phase_logs/README.md#start-here-four-engineering-stories) |
| **A RAG deep dive** | [RAG README](RAG/README.md) → [PROJECT_SHOWCASE](RAG/PROJECT_SHOWCASE.md) → [RAG_ARCHITECTURE](RAG/RAG_ARCHITECTURE.md) → [ENGINEERING_DECISIONS](RAG/ENGINEERING_DECISIONS.md) → [FINAL_METRICS_SUMMARY](RAG/metrics/FINAL_METRICS_SUMMARY.md) → [R6 gate and judge fix](phase_logs/R6_gate_and_judge_fix_asbuilt.md) |

---

## Product

What Atlas is for, what was planned, and what shipped.

| Document | What it is |
|---|---|
| [product/PRD.md](product/PRD.md) | The original pre-build product spec (May 2026), published with light edits: vision, three roles, six core features, MVP and out-of-scope lists, 11 user stories, 28 functional and 18 non-functional requirements, 10 success metrics |
| [product/domain-study-sap-concur.md](product/domain-study-sap-concur.md) | How SAP Concur works, from public sources only: T&E basics, the product family, the travel → expense → approval → reimbursement → audit loop, policy concepts, receipt capture, announced AI direction, and what the study shaped in Atlas |
| [product/concur-to-atlas-map.md](product/concur-to-atlas-map.md) | Each Concur concept mapped to its Atlas equivalent, with code paths and what Atlas simplifies (simulated inventory, card feed and payments, static FX, no delegates) |
| [product/requirements-traceability.md](product/requirements-traceability.md) | PRD → as-built matrix: 85 items, each with a status and evidence (code, test or phase log), plus the features built beyond the original PRD |

## Explanation: architecture and design

| Document | What it is |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The whole-application map: system context, containers, request lifecycle, an expense-lifecycle sequence, a code map, invariants with the tests that hold them, and boundaries |
| [design/technical-design.md](design/technical-design.md) | The original technical design (May 2026) annotated with as-built notes, including the as-built API surface of 80 `/api/v1` endpoints |
| [design/database-schema.md](design/database-schema.md) | All 24 tables, two ER diagrams, migrations 0001–0025, indexes and integrity rules |
| [design/ui-ux.md](design/ui-ux.md) | UX principles, the app shell and the key screens, with as-built notes and screenshots |
| [design/security-model.md](design/security-model.md) | Threat model for a public, shared-account demo; every control with its code path and tests; residual risks |
| [../SECURITY.md](../SECURITY.md) | How to report a vulnerability, what is in scope, and the rules for testing the shared demo |

### Architecture decision records

| Document | What it is |
|---|---|
| [adr/README.md](adr/README.md) | Index of the application ADRs and the retrieval-engine ADRs, with status and date |
| [adr/0001-modular-monolith.md](adr/0001-modular-monolith.md) | A modular monolith with one-way package layering, not microservices |
| [adr/0002-provider-adapters-with-local-fallbacks.md](adr/0002-provider-adapters-with-local-fallbacks.md) | Every external dependency behind a provider adapter with a deterministic local fallback |
| [adr/0003-deterministic-core-routed-agents.md](adr/0003-deterministic-core-routed-agents.md) | A deterministic core where the LLM is a routed exception, not the default path |
| [adr/0004-llm-free-approval-agent.md](adr/0004-llm-free-approval-agent.md) | The approval agent uses no LLM, has a fixed low-risk gate and never auto-rejects |
| [adr/0005-commit-pinned-deploy-verification.md](adr/0005-commit-pinned-deploy-verification.md) | Deploy only the commit CI tested, and verify it by the SHA each service reports |
| [adr/0006-application-level-tenancy.md](adr/0006-application-level-tenancy.md) | Tenant isolation in the application, without row-level security |
| [adr/0007-optimistic-concurrency-and-idempotency-keys.md](adr/0007-optimistic-concurrency-and-idempotency-keys.md) | Version compare-and-swap (`If-Match`) and idempotency keys for safe writes and retries |
| [adr/0008-pdf-rasterise-only.md](adr/0008-pdf-rasterise-only.md) | PDF receipts accepted only by rasterising them in a contained child process; the PDF is never stored |
| [RAG/adr/](RAG/adr/) | Retrieval-engine ADR-001 to ADR-005: hybrid retrieval, reciprocal rank fusion, cross-encoder reranking, confidence calibration, single audit write |

### RAG engine

| Document | What it is |
|---|---|
| [RAG/README.md](RAG/README.md) | Hub for the Atlas Retrieval Engine, with its own reading path |
| [RAG/PROJECT_SHOWCASE.md](RAG/PROJECT_SHOWCASE.md) | The two-minute overview of the engine: problem, design, results |
| [RAG/RAG_ARCHITECTURE.md](RAG/RAG_ARCHITECTURE.md) | The full pipeline, from indexing to the calibrated abstention gate and the audit write |
| [RAG/ENGINEERING_DECISIONS.md](RAG/ENGINEERING_DECISIONS.md) | How each failure led to a design decision, including the R6 measurement story |
| [RAG/PERFORMANCE_PROFILE.md](RAG/PERFORMANCE_PROFILE.md) | Latency: what was measured and what was deferred |
| [RAG/FUTURE_WORK.md](RAG/FUTURE_WORK.md) | Techniques considered and deferred, with the reason |
| [RAG/GLOSSARY.md](RAG/GLOSSARY.md) | Plain-language definitions of the retrieval and evaluation terms |
| [RAG/diagrams/](RAG/diagrams/) | Mermaid sources and rendered PNGs of the engine's diagrams |
| [RAG/research/README.md](RAG/research/README.md) | Pointer to the research survey and how it relates to the build |
| [RAG_RESEARCH_REPORT.md](RAG_RESEARCH_REPORT.md) | The research survey written before the engine was built; its figures are targets or published claims, not Atlas measurements |

## Quality

| Document | What it is |
|---|---|
| [quality/TESTING.md](quality/TESTING.md) | Test strategy and current results: the tiers, the CI job graph, AI and RAG metrics, the determinism strategy and known test caveats |
| [quality/delivery-process.md](quality/delivery-process.md) | How the work was planned, gated, logged, reviewed, released and rolled back |
| [../evals/README.md](../evals/README.md) | How every evaluation tier and keyed target runs: datasets, thresholds, make targets |
| [../evals/reports/README.md](../evals/reports/README.md) | What the (regenerated, uncommitted) eval reports contain and what CI gates |
| [RAG/metrics/FINAL_METRICS_SUMMARY.md](RAG/metrics/FINAL_METRICS_SUMMARY.md) | The canonical RAG numbers, each with its dataset, run condition and source |

## How-to guides and operations

| Document | What it is |
|---|---|
| [guides/local-setup.md](guides/local-setup.md) | Run Atlas locally (full Docker or hybrid), run every test tier, troubleshoot, verify in five minutes |
| [guides/deployment.md](guides/deployment.md) | Staging topology, the commit-pinned CI/CD pipeline and its verification, where secrets live, boot-time migrations, cold starts |
| [runbooks/rollback.md](runbooks/rollback.md) | Put staging back on a known-good release (Vercel, Render, Neon), with the drilled timings |
| [runbooks/demo-reset.md](runbooks/demo-reset.md) | Restore the demo organisation to its seed baseline, by hand or on the daily schedule |
| [runbooks/elapsed-trip-sweep.md](runbooks/elapsed-trip-sweep.md) | Preview, run or disable the boot-time completion of approved trips that have ended |
| [runbooks/rate-limit-redis.md](runbooks/rate-limit-redis.md) | Move rate-limit counters from process memory to a shared Redis, verify, roll back |
| [runbooks/staging-test-data-cleanup.md](runbooks/staging-test-data-cleanup.md) | Remove old acceptance-round records from staging, taking a database branch backup first |
| [runbooks/key-rotation.md](runbooks/key-rotation.md) | Rotate each secret in order and verify it without printing the value |
| [runbooks/public-demo-checklist.md](runbooks/public-demo-checklist.md) | The go-live checklist for exposing the demo publicly |

## Reference

| Document | What it is |
|---|---|
| [guides/configuration.md](guides/configuration.md) | Every environment variable by area, with code defaults, startup validation rules and names that do nothing |
| [staging_env_map.md](staging_env_map.md) | Every staging variable by platform (Render, Vercel, GitHub), names and placeholders only |
| [env_audit_p8_t3.md](env_audit_p8_t3.md) | The configuration audit: which variables the code reads, who declares them, where each must be set |
| [assets/README.md](assets/README.md) | How the README screenshots and demo GIF were captured, and the sample receipts |

## Build history

| Document | What it is |
|---|---|
| [phase_logs/README.md](phase_logs/README.md) | Index of all 101 as-built logs (phases 1–12 and the RAG engine R0–R6), starting with four engineering stories |
| [archive/README.md](archive/README.md) | Planning documents kept for comparison with what was built |
| [archive/RAG_MASTERPLAN.md](archive/RAG_MASTERPLAN.md) | The original R0–R6 retrieval-engine hardening plan |
| [archive/phase8_preflight_remediation_plan.md](archive/phase8_preflight_remediation_plan.md) | The June 2026 pre-deployment remediation plan, with its verification results |

---

Author: Garv Singh Saini. Released under the [MIT License](../LICENSE).

**Disclaimer.** Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.
