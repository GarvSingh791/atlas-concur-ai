# Phase logs

The as-built record of how Atlas was built: one log per task or track, written when the work
was finished, from the code. For reviewers who want the reasoning and the evidence behind a
feature, not just the feature. There are 102 logs; this page indexes all of them.

How the logs fit into the overall process (task IDs, Validate gates, PRs, deploys) is described
in [delivery-process.md](../quality/delivery-process.md).

---

## Start here: four engineering stories

If you read four logs, read these.

| Story | Log | Why it is worth reading |
|---|---|---|
| **A green CI that hid a broken live path** | [`R6_gate_and_judge_fix_asbuilt.md`](R6_gate_and_judge_fix_asbuilt.md) | The first keyed run grounded 0 of 145 answerable questions while CI was green: a 0.70 confidence floor was applied to a calibrated score whose threshold is about 0.039. Fixed with a scale-aware floor (0 → 113/145). A second defect was in the evaluator, not the model: faithfulness moved from 0.775 to 0.991 by fixing the judge. Follow-up: the [C7R re-run](C7R_faithfulness_rerun.md) (0.981 @ 74%, 108/145) after the per-sentence support gate. |
| **Deploying the commit you tested, and proving it** | [`phase_8_p8_t4_cicd_pipeline.md`](phase_8_p8_t4_cicd_pipeline.md) | The original health poll was a false green (it passed in 54 s against the old release). The fix pins the Render deploy to the tested SHA and waits until both services report it. Seven gates, an adversarial review, a timed rollback drill (Render 77 s, Vercel 32 s), and the finding that a Render rollback cannot cross a migration. |
| **Making a public demo safe to share** | [`phase_11_public_demo_security_integration.md`](phase_11_public_demo_security_integration.md) | Rate limiting with login backoff, upload hardening against oversized and disguised files, protected demo accounts and a reset tool, a CI artifact that could leak tokens, and a secret scan over the full history. The integration log records each review finding, how it was closed, the gate counts and the residual risks. |
| **Test tiers that measure AI as classification** | [`P7-T3-ai-output-validation.md`](P7-T3-ai-output-validation.md) | A golden-dataset harness that scores extraction, matching and policy decisions with per-class precision and recall, including a hard gate that no real violation may pass silently. Companion logs: [unit tests](P7-T1-unit-tests.md), [API contract matrix](P7-T2-api-contract-tests.md), [Playwright e2e](P7-T4-e2e-tests.md). |

---

## What an as-built log contains

The logs vary in format across the build (the earliest are short; later ones are longer), but
each records the same things:

- **What was built:** the files and modules, and what was deliberately left out of scope.
- **Reconcile findings:** where the plan's assumptions did not match the code, and what was done
  instead. The rule throughout is that the code wins and the difference is written down.
- **Decisions:** the options considered and the one taken, with the reason.
- **Verification evidence:** the gate for the task, the commands run, test counts, measured
  metrics, and for deploy work the live checks.
- **Residual risks, known limits and carry-forward items:** what the change did not close,
  which later phases picked up.

Where a later integration log disagrees with a track log, the integration log wins and says so.
Branch names, commit hashes and dates in the logs are from the time of writing; the logs are
not rewritten as the code moves on.

**Design documents named in the logs.** The logs (and some code comments) cite the original
design documents by their working file names. Their published versions are:

| Name in a log | Published version |
|---|---|
| `PRD.md` | [product/PRD.md](../product/PRD.md) |
| `TECHNICAL_DESIGN.md` | [design/technical-design.md](../design/technical-design.md) |
| `DATABASE_SCHEMA.md` | [design/database-schema.md](../design/database-schema.md) |
| `UI_UX.md` | [design/ui-ux.md](../design/ui-ux.md) |
| `MASTER_EXECUTION_PLAN.md` | Not published; its structure (phases, task IDs, Validate gates) is described in [delivery-process.md](../quality/delivery-process.md) |
| `research.md` | Not published; the public-source domain study is [product/domain-study-sap-concur.md](../product/domain-study-sap-concur.md) |
| `RAG_MASTERPLAN.md` | [archive/RAG_MASTERPLAN.md](../archive/RAG_MASTERPLAN.md) |

---

## Phase 1 — Project bootstrap

| Log | What it records |
|---|---|
| [`Phase1_P1-T1.md`](Phase1_P1-T1.md) | Monorepo skeleton, design documents placed under `docs/`, first README and a complete `.env.example` |
| [`Phase1_P1-T2.md`](Phase1_P1-T2.md) | FastAPI skeleton: typed settings, `/api/v1` router, `GET /health`, correlation-ID middleware, global error handler |
| [`Phase1_P1-T3.md`](Phase1_P1-T3.md) | Next.js App Router skeleton in strict TypeScript, route groups, Tailwind tokens and shadcn/ui |
| [`Phase1_P1-T4.md`](Phase1_P1-T4.md) | Docker Compose stack (Postgres + pgvector, MinIO, backend, frontend), non-root images, boot-time DB check |
| [`Phase1_P1-T5.md`](Phase1_P1-T5.md) | Lint, format, type-check and test tooling for both stacks, and the first hermetic CI workflow; closes phase 1 |

## Phase 2 — Core backend

| Log | What it records |
|---|---|
| [`P2-T1_db_session_base_conventions.md`](P2-T1_db_session_base_conventions.md) | One declarative base with a naming convention, six convention mixins, the shared session factory and `get_db` |
| [`P2-T2_cross_cutting_core.md`](P2-T2_cross_cutting_core.md) | Error envelope and handlers, correlation IDs in JSON logs, pagination/filter/sort helpers, the single audit write path |
| [`P2-T3_auth_system.md`](P2-T3_auth_system.md) | Identity vertical slice: identity models, first migration, JWT access and refresh tokens, password hashing |
| [`P2-T4_rbac_record_scoping.md`](P2-T4_rbac_record_scoping.md) | Permission catalogue, `require_permission` / `require_role` dependencies, tenant-first record scoping (`scope_query`) |
| [`P2-T5_user_management_api.md`](P2-T5_user_management_api.md) | `GET /users` and `GET /users/{id}` with RBAC, scoping, pagination and a safe response schema |
| [`P2-T6_trips_crud.md`](P2-T6_trips_crud.md) | Trips and segments: models, migration, legal state transitions, five scoped endpoints with audit |
| [`P2-T7_expenses_reports_crud.md`](P2-T7_expenses_reports_crud.md) | Expense reports and entries: models, migration `0003`, state machine, server-derived totals; closes phase 2 |

## Phase 3 — Database

| Log | What it records |
|---|---|
| [`P3-T1_remaining_models.md`](P3-T1_remaining_models.md) | Eleven remaining ORM models in dependency order and migration `0004` (pgvector) |
| [`P3-T2_migration_chain_verification.md`](P3-T2_migration_chain_verification.md) | Verification of the per-slice chain `0001`–`0004` and a zero-drift guard, instead of a squashed initial migration |
| [`P3-T3_indexing_strategy.md`](P3-T3_indexing_strategy.md) | Index-only migration `0005` (foreign-key, composite, GIN and HNSW indexes) declared model-first |
| [`P3-T4_integrity_rules.md`](P3-T4_integrity_rules.md) | Migration `0006` constraints (temporal check, one active policy, idempotency keys) and application-level integrity machinery |
| [`P3-T5_seed_data.md`](P3-T5_seed_data.md) | The idempotent seed: demo organisation, users, policy, inventory and grounding data; closes phase 3 |

## Phase 4 — AI system

| Log | What it records |
|---|---|
| [`P4-T1_provider-abstractions.md`](P4-T1_provider-abstractions.md) | The `services/` adapter layer: LLM, embeddings, OCR, storage and mock inventory behind swappable interfaces |
| [`P4-T2_decision_logging_guardrails.md`](P4-T2_decision_logging_guardrails.md) | The decision-log writer (one row per AI decision) and the grounding and role-bound guardrail contract |
| [`P4-T3_reflection_critic.md`](P4-T3_reflection_critic.md) | Bounded generator-critic reflection loop that checks a hypothesis against grounding facts (the "Paris, France vs Paris, Texas" case) |
| [`P4-T4_tool_layer.md`](P4-T4_tool_layer.md) | Five read-only grounding tools that return typed, citable results |
| [`P4-T5_knowledge_base_embeddings.md`](P4-T5_knowledge_base_embeddings.md) | Policy chunker and idempotent, provider-backed embedding re-indexer |
| [`P4-T6_retrieval_flow.md`](P4-T6_retrieval_flow.md) | Grounded retrieval with a similarity threshold and a "silent" outcome instead of a guess |
| [`P4-T7_intent_classification_routing.md`](P4-T7_intent_classification_routing.md) | The orchestrator: intent classification and routing between deterministic handlers and agents |
| [`P4-T8_policy_agent.md`](P4-T8_policy_agent.md) | Policy agent: deterministic rule evaluation, grounded Q&A, rule-authoring assist that creates drafts only |
| [`P4-T9_travel_agent.md`](P4-T9_travel_agent.md) | Travel agent: slot-filling intent, inventory search, per-option policy checks, compliance-first ranking with no LLM call on the decision path |
| [`P4-T10_expense_agent.md`](P4-T10_expense_agent.md) | Expense agent: smart match, report assembly, pre-submit policy check |
| [`P4-T11_receipt_pipeline.md`](P4-T11_receipt_pipeline.md) | Receipt pipeline: deterministic OCR core, per-field confidence routing at 0.70, gap-fill, location reflection |
| [`P4-T12_approval_agent.md`](P4-T12_approval_agent.md) | Approval agent: deterministic routing, auto-approve in policy, escalate otherwise |
| [`P4-T13_assistant_endpoints.md`](P4-T13_assistant_endpoints.md) | Assistant endpoints and the composition root that wires agents into the API; closes phase 4 |

## Phase 5 — Frontend

| Log | What it records |
|---|---|
| [`Phase5_P5-T1.md`](Phase5_P5-T1.md) | Design tokens and primitives, including the trust components (provenance badge, confidence, citation chip) |
| [`Phase5_P5-T2.md`](Phase5_P5-T2.md) | Typed API client (envelope unwrap, 401 → refresh once → retry), DTO mirrors, auth and session |
| [`Phase5_P5-T3.md`](Phase5_P5-T3.md) | Role-aware app shell, sidebar driven by permissions, responsive collapse |
| [`Phase5_P5-T4.md`](Phase5_P5-T4.md) | Role-scoped dashboard, plus the analytics overview endpoint it needed (a recorded scope expansion) |
| [`Phase5_P5-T5.md`](Phase5_P5-T5.md) | Travel Planner: streamed (SSE) conversation, recommendations and the frame types it consumes |
| [`Phase5_P5-T6.md`](Phase5_P5-T6.md) | Receipt upload and extraction review with per-field confidence, provenance and inline correction |
| [`P5-T7_expense_dashboard.md`](P5-T7_expense_dashboard.md) | Expense report list and detail, pre-submit flags, status tracking |
| [`P5-T8_approval_dashboard.md`](P5-T8_approval_dashboard.md) | Role-scoped approval queue and decision panel, plus the approval read endpoints it needed |
| [`P5-T9_analytics_policy.md`](P5-T9_analytics_policy.md) | Finance analytics and policy/admin screens, plus policy read, activation and aggregate endpoints |
| [`P5-T10_assistant.md`](P5-T10_assistant.md) | Reusable AI assistant slide-out with tool-activity display; closes phase 5 |

## Phase 6 — Integration

| Log | What it records |
|---|---|
| [`Phase6_P6-T1.md`](Phase6_P6-T1.md) | Every screen on live APIs; two seam bugs that made the app look finished while it did not work against real APIs |
| [`Phase6_P6-T2.md`](Phase6_P6-T2.md) | Backend → agent delegation proven for all six agent paths, each writing a scoped decision-log row |
| [`Phase6_P6-T3.md`](Phase6_P6-T3.md) | Adversarial cross-role scoping suite (42 tests) and the receipt → expense → report → trip lineage chain |
| [`Phase6_P6-T4.md`](Phase6_P6-T4.md) | OCR, inventory and RAG through their adapters; deterministic-first routing measured (8 deterministic, 4 LLM, zero LLM calls on deterministic cases) |
| [`Phase6_P6-T5.md`](Phase6_P6-T5.md) | Fault injection at the provider seam: failures degrade to "needs review", never a broken flow; closes phase 6 |

## Phase 7 — Testing

| Log | What it records |
|---|---|
| [`P7-Foundation-shared-test-infra.md`](P7-Foundation-shared-test-infra.md) | Shared pytest infrastructure: migrated schema fixture, rollback session, role clients, factories, deterministic AI wiring |
| [`P7-T1-unit-tests.md`](P7-T1-unit-tests.md) | Backend unit suite (102 tests) with mutation checks |
| [`P7-T2-api-contract-tests.md`](P7-T2-api-contract-tests.md) | The API contract matrix (32 endpoints at the time; 80 today) plus validation, scoping, pagination, idempotency and state tests |
| [`P7-T3a-gemini-provider.md`](P7-T3a-gemini-provider.md) | OpenRouter → Gemini LLM adapter behind the existing interface, off by default, degrading to the deterministic path on failure |
| [`P7-T3-ai-output-validation.md`](P7-T3-ai-output-validation.md) | AI-eval harness: golden datasets, M-A1/M-A2/M-A3 and RAG metrics, thresholds, snapshots, CI wiring |
| [`P7-T4-e2e-tests.md`](P7-T4-e2e-tests.md) | Playwright suite per role against a live backend with local providers (18/18, re-runnable); closes phase 7 |

## Phase 8 — Deployment, CI/CD and completion

| Log | What it records |
|---|---|
| [`phase_8_p8_t1_docker_images.md`](phase_8_p8_t1_docker_images.md) | Production images, self-migrating backend entrypoint, compose reproducibility from zero (14/14 gates) |
| [`phase_8_p8_t2_cloud_topology.md`](phase_8_p8_t2_cloud_topology.md) | Staging on Vercel, Render, Neon and Cloudflare R2; extensions moved into migration `0001` |
| [`phase_8_p8_t3_env_variables.md`](phase_8_p8_t3_env_variables.md) | The staging variable map: every variable to its platform, no hardcoded secrets |
| [`phase_8_p8_t3_dashboard_verification.md`](phase_8_p8_t3_dashboard_verification.md) | Read-only check of the Render, Vercel, Neon, R2 and GitHub settings against the spec; R2 token rotation; follow-ups closed |
| [`phase_8_p8_t4_cicd_pipeline.md`](phase_8_p8_t4_cicd_pipeline.md) | Commit-pinned deploy, commit verification, gates G1–G7, rollback drill |
| [`P8_r2_checksum_hardening_asbuilt.md`](P8_r2_checksum_hardening_asbuilt.md) | Defensive S3 client settings for R2 (checksums, SigV4), and a stale carry-forward item checked and closed |
| [`phase_8_completion_c1_c4.md`](phase_8_completion_c1_c4.md) | C1–C4: deterministic categorisation, signed receipt image access, correlation ID on decision logs, finance audit page |
| [`phase_8_completion_c5_c6.md`](phase_8_completion_c5_c6.md) | C5–C6: README rewrite, and a reproducibility drill from a fresh clone and an empty Neon branch using only the README |
| [`phase_8_completion_c7_fixes.md`](phase_8_completion_c7_fixes.md) | C7: fixes for the 27 defects found in the staging acceptance run (8 high, 13 medium, 6 low) |
| [`phase_8_completion_c7r_fixes.md`](phase_8_completion_c7r_fixes.md) | C7R: re-acceptance fixes, including the sentence-level support gate for policy answers |

## Phase 9 — Product gaps

| Log | What it records |
|---|---|
| [`phase_9_product_gaps_lifecycle.md`](phase_9_product_gaps_lifecycle.md) | Simulated reimbursement, withdrawing a returned report, trip completion; migration `0016` |
| [`phase_9_product_gaps_policy.md`](phase_9_product_gaps_policy.md) | Entry-level policy rules, ground-transport sub-types, per-rule change history, and several policy review rounds |
| [`phase_9_product_gaps_policy2.md`](phase_9_product_gaps_policy2.md) | Spend caps, gift disclosure and a new mileage feature; migration `0019` |
| [`phase_9_product_gaps_shell.md`](phase_9_product_gaps_shell.md) | Notifications, global search, users admin, removal of a dead OCR option; migration `0020` |
| [`phase_9_product_gaps_team.md`](phase_9_product_gaps_team.md) | Manager team view, finance filters, CSV exports |
| [`phase_9_product_gaps_fxcard.md`](phase_9_product_gaps_fxcard.md) | Currency conversion with labelled simulated rates, card feed UI, relative dates in the planner |
| [`phase_9_product_gaps_integration.md`](phase_9_product_gaps_integration.md) | Merge order and conflicts of the four tracks, cross-track fixes and the integration gate |

## Phase 10 — Known limits

| Log | What it records |
|---|---|
| [`phase_10_known_limits_grade.md`](phase_10_known_limits_grade.md) | Traveller grades so AIR-04 (business class for SVPs and above) is decided from the grade of record; migration `0021` |
| [`phase_10_known_limits_ground.md`](phase_10_known_limits_ground.md) | Worst-case checking of ground-transport lines that no trusted evidence identifies |
| [`phase_10_known_limits_identity.md`](phase_10_known_limits_identity.md) | Organisation-aware login and emails unique per organisation; migration `0022` |
| [`phase_10_known_limits_refile.md`](phase_10_known_limits_refile.md) | Re-filing lineage to stop over-flagging, and operator control of the elapsed-trip sweep; migration `0023` |
| [`phase_10_known_limits_integration.md`](phase_10_known_limits_integration.md) | Merge of the four tracks, review fixes, gates and residual risks handed to phase 11 |

## Phase 11 — Public-demo security

| Log | What it records |
|---|---|
| [`phase_11_public_demo_security_ratelimit.md`](phase_11_public_demo_security_ratelimit.md) | Dependency-free request limiter, rate specs validated at startup, a bound on bcrypt work per login |
| [`phase_11_public_demo_security_uploads.md`](phase_11_public_demo_security_uploads.md) | Streamed size caps, content-type sniffing, decompression-bomb limits, quotas for receipt uploads |
| [`phase_11_public_demo_security_demo.md`](phase_11_public_demo_security_demo.md) | Demo mode protecting the shared accounts and seeded rules, and the demo reset tool |
| [`phase_11_public_demo_security_ops.md`](phase_11_public_demo_security_ops.md) | A CI report that could leak tokens, the staging cleanup tool, the key rotation runbook, secret scan and env audit |
| [`phase_11_public_demo_security_integration.md`](phase_11_public_demo_security_integration.md) | Merge of the tracks, six review items closed, gate counts, residual risks, live proxy-count check |
| [`phase_11_rate_limit_hardening.md`](phase_11_rate_limit_hardening.md) | Follow-up: backoff keys that cannot be minted by varying the org code, a per-email distributed-attack guard, overflow buckets |

## Phase 12 — Follow-ups

| Log | What it records |
|---|---|
| [`phase_12_followups_accounts.md`](phase_12_followups_accounts.md) | Password reset, invitations and email verification with a `console` / `smtp` email provider; migration `0024` |
| [`phase_12_followups_grades.md`](phase_12_followups_grades.md) | AIR-07, LOD-05 and NR-01 enforced from grades, and approvals routed to an approver of the named grade; migration `0025` |
| [`phase_12_followups_pdf.md`](phase_12_followups_pdf.md) | PDF receipts rendered to one PNG in a contained child process; the PDF is never stored |
| [`phase_12_followups_redis.md`](phase_12_followups_redis.md) | Optional Redis store so rate-limit counters are shared across workers and instances |
| [`phase_12_followups_autoreset.md`](phase_12_followups_autoreset.md) | In-process scheduled reset of the demo organisation, off by default |
| [`phase_12_followups_uinits.md`](phase_12_followups_uinits.md) | Ten small UI fixes from browser QA, each with a test proven to fail without the fix |

## RAG engine — R0 to R6

The retrieval engine has its own documentation in [`docs/RAG/`](../RAG/README.md); these logs are
its build record. Canonical numbers: [`FINAL_METRICS_SUMMARY.md`](../RAG/metrics/FINAL_METRICS_SUMMARY.md).

| Log | What it records |
|---|---|
| [`R0-T1_asbuilt.md`](R0-T1_asbuilt.md) | Reconcile of the existing retrieval pipeline before the multi-document work |
| [`R0-T2_corpus.md`](R0-T2_corpus.md) | The four-document synthetic T&E corpus (policy, FAQ, how-to, reference; 208 units) |
| [`R0-T3_T6_asbuilt.md`](R0-T3_T6_asbuilt.md) | Chunk metadata (migration `0009`), embedding, retiring the old stub, the gold set, and the dense-only baseline (recall@10 0.727) |
| [`R1-T1_T2_asbuilt.md`](R1-T1_T2_asbuilt.md) | Lexical `tsvector` index and parallel dense + sparse retrieval |
| [`R1-T3_T5_asbuilt.md`](R1-T3_T5_asbuilt.md) | Reciprocal Rank Fusion behind the unchanged retrieval interface; recall@10 0.727 → 0.819 |
| [`R2-T1_T2_asbuilt.md`](R2-T1_T2_asbuilt.md) | Rerank provider and two-stage pipeline (part 1), then the hosted cross-encoder, Platt calibration and live results (part 2) |
| [`R3-T1_T2_asbuilt.md`](R3-T1_T2_asbuilt.md) | Calibrated abstention: τ\* chosen from a risk-coverage sweep instead of a hand-tuned cosine cut-off |
| [`R3-T3_T5_asbuilt.md`](R3-T3_T5_asbuilt.md) | Confidence display fix, a deterministic abstention message with an escalation contact, the abstention evaluation contract |
| [`R4-T1_T2_asbuilt.md`](R4-T1_T2_asbuilt.md) | Contextual retrieval mechanism (document-situating prefixes), off by default, measured on the local embedder |
| [`R4-T3_T4_asbuilt.md`](R4-T3_T4_asbuilt.md) | Embedder-upgrade machinery, live matrix runner, recalibration; adoption decision deferred pending a key |
| [`R4-live-matrix_asbuilt.md`](R4-live-matrix_asbuilt.md) | OpenRouter embeddings adapter, reranker moved to Novita, and why the current embedder was retained |
| [`R5-T1_T2_asbuilt.md`](R5-T1_T2_asbuilt.md) | Latency-gated HyDE and per-sentence, document-qualified citation enforcement (both off by default) |
| [`R5-T3_T4_asbuilt.md`](R5-T3_T4_asbuilt.md) | Deterministic entailment check before the audit write, and the keyed faithfulness harness over 145 answerable queries |
| [`R6-T1_T2_asbuilt.md`](R6-T1_T2_asbuilt.md) | Latency levers recorded honestly as forward-looking at this corpus size, and the grounding-confidence UI |
| [`R6-T3_T4_asbuilt.md`](R6-T3_T4_asbuilt.md) | The full retrieval story in the single decision-log write, and the regression floors gated in CI |
| [`R6_gate_and_judge_fix_asbuilt.md`](R6_gate_and_judge_fix_asbuilt.md) | The scale-aware citation floor (grounding 0 → 113/145) and the statement-level faithfulness judge (0.775 → 0.991) |
| [`C7R_faithfulness_rerun.md`](C7R_faithfulness_rerun.md) | The keyed faithfulness re-run after the C7R per-sentence support gate: 0.981 @ 74% grounded (108/145), the current canonical figure |
