# R0-T1 — As-Built Reconcile (RAG Masterplan, Phase R0)

Reconcile of the live codebase before R0-T2. **Code wins over plan.** Confirms the
multi-document feasibility outcome and records two forward-notes that R0-T4 depends on.

## Pipeline (all claimed paths confirmed)

| Concern | Path | Finding |
|---|---|---|
| Policy seed text | [policy_content.py](../../database/seeds/policy_content.py) | Legacy **8-clause / 7-rule** stub ("Atlas Global Travel & Expense Policy"); clause_ref scheme `AIR-CABIN`, `HOTEL-CAP`, … (NOT the new `AIR-01` scheme). |
| Seed runner | [seed_all.py](../../database/seeds/seed_all.py) | `_seed_policy()` idempotent via query-first guard on `(org_id, name, version)`; org **"Atlas Demo Corp"**; seeds policy + rules + embeddings in one txn. |
| Retrieval tool | [policy_retrieval.py](../../ai_agents/tools/policy_retrieval.py) | Cosine ANN over `policy_embeddings`; org-scope **JOIN `policies p ON p.id = pe.policy_id WHERE p.organization_id = :org`**. |
| Grounding/abstention | [retrieval.py](../../ai_agents/rag/retrieval.py) | Silent on no-chunks / top-similarity < threshold; confidence rescale `0.5 + 0.5·(top−τ)/(1−τ)`. |
| Threshold | [base.py:105](../../config/settings/base.py#L105) | `RETRIEVAL_SIMILARITY_THRESHOLD: float = 0.20` (local-embedder calibration). |
| QA agent | [policy_qa.py](../../ai_agents/agents/policy_qa.py) | Hypothesizer + critic over retrieved clauses. |
| Embedder | [factory.py](../../services/embeddings_provider/factory.py) / local.py / real.py | `EMBEDDINGS_PROVIDER` switch: `local` = deterministic blake2b **1536-d** (CI); `openai` = text-embedding-3-small. `EMBEDDING_DIMENSION=1536`. |
| `policy_embeddings` | [policy_embedding.py](../../database/models/policy_embedding.py) | FK `policy_id → policies.id` (CASCADE); `embedding VECTOR(1536)`; **no organization_id** (org via parent). |
| `policy_rules` | [policy_rule.py](../../database/models/policy_rule.py) | FK `policy_id → policies.id`; **no organization_id**. |

## Multi-document schema outcome → (b) small additive column

[policies](../../database/models/policy.py) **already supports multiple documents per org**:
unique `(organization_id, name, version)` + partial-unique `(organization_id, name) WHERE is_active`.
Distinct `name` per document ⇒ no constraint conflict. It has `name` (≙ title) and `document_text`
but **no type discriminator**. R0-T2 adds a small additive nullable enum column `document_type`
(`policy_document_type {policy, faq, howto, reference}`) — migration **0008**, head was **0007**.
No new table; vector dimensionality untouched. (Matches Fence 7 / Masterplan §11.)

## Decisions (confirmed with maintainer)

1. **Doc (a) source** — PORT from the research report Part 4:
   [RAG_RESEARCH_REPORT.md](../RAG_RESEARCH_REPORT.md)
   lines 193–337 (Helios **TEP-FY26 v4.0**, ~98 clauses, `AIR-01/LOD-01/MEAL-01` scheme). Project's own
   synthetic text ⇒ no third-party-text fence breach.
2. **Legacy 8-clause stub** — **keep additive** through R0-T2; retrieval keeps serving it. Retire at R0-T4.
3. **`document_type`** — Postgres **ENUM** (matches the `PolicyRuleType`/`PolicyAppliesTo`/`PolicyRuleStatus`
   idiom), nullable; `name` reused as title.

## ⚠ Forward-notes for R0-T4 (flag only — no stub change in R0-T2)

- **Retrieval does NOT filter `policies.is_active`.** The dense SQL
  ([policy_retrieval.py:107-109](../../ai_agents/tools/policy_retrieval.py#L107-L109)) JOINs on
  `organization_id` only. Therefore, **setting the stub `is_active=False` will NOT remove it from
  retrieval.** To retire the legacy stub at R0-T4 without a mixed `AIR-CABIN` + `AIR-01` corpus, its
  **`policy_embeddings` rows must be physically DELETED** (CASCADE via deleting the stub `policies`
  row, or an explicit delete), not merely deactivated.
- R0-T2 seeds the 4 Helios document **rows + `document_text` only** — **no `policy_rules`, no
  `policy_embeddings`**. Chunking is R0-T3; embedding (1536-d, idempotent) is R0-T4. Until R0-T4 runs,
  retrieval is served entirely by the legacy stub's 8 embeddings (expected interim state).
