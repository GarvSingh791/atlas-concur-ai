# Architecture decision records

Short records of the decisions that shaped Atlas: what was decided, what else was on the table, and what it cost. They are for engineers who want the reasoning behind the code, and for reviewers who want to see how trade-offs were weighed. Each record links to the code and tests that carry the decision out.

Application-level decisions are numbered `0001`, `0002`, … in this folder. The retrieval engine has its own five records in [`docs/RAG/adr/`](../RAG/adr/), numbered `ADR-001` to `ADR-005`.

## Format

Each record uses the same headings (Nygard / MADR style): **Status**, **Date**, **Context**, **Decision**, **Alternatives considered**, **Consequences**, **Evidence**. The application records were written up on 2026-11-19 from the phase logs and the code; the **Date** field is when the decision was made, taken from the phase log that introduced it.

## Application decisions

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-modular-monolith.md) | Build a modular monolith with one-way package layering, not microservices | Accepted | 2026-05-31 |
| [0002](0002-provider-adapters-with-local-fallbacks.md) | Put every external dependency behind a provider adapter with a deterministic local fallback | Accepted | 2026-06-13 |
| [0003](0003-deterministic-core-routed-agents.md) | Deterministic core with routed agents: the LLM is a routed exception, not the default path | Accepted | 2026-06-13 |
| [0004](0004-llm-free-approval-agent.md) | The approval agent uses no LLM, has a fixed low-risk gate and never auto-rejects | Accepted | 2026-06-14 |
| [0005](0005-commit-pinned-deploy-verification.md) | Deploy only the commit CI tested, and verify it by the SHA each service reports | Accepted | 2026-11-07 |
| [0006](0006-application-level-tenancy.md) | Enforce tenant isolation in the application (`scope_query`), without row-level security | Accepted | 2026-06-12 |
| [0007](0007-optimistic-concurrency-and-idempotency-keys.md) | Use version compare-and-swap (`If-Match`) and idempotency keys for safe writes and retries | Accepted | 2026-06-12 |
| [0008](0008-pdf-rasterise-only.md) | Accept PDF receipts only by rasterising them in a contained child process; never store or serve the PDF | Accepted (supersedes the phase 11 PDF refusal) | 2026-11-15 |

## Retrieval engine decisions

| # | Decision |
|---|---|
| [ADR-001](../RAG/adr/ADR-001-hybrid-retrieval.md) | Hybrid retrieval: dense pgvector plus sparse PostgreSQL full-text |
| [ADR-002](../RAG/adr/ADR-002-reciprocal-rank-fusion.md) | Reciprocal Rank Fusion (k = 60) to merge the two candidate lists |
| [ADR-003](../RAG/adr/ADR-003-cross-encoder-reranking.md) | Cross-encoder reranking of the fused candidates |
| [ADR-004](../RAG/adr/ADR-004-confidence-calibration.md) | Calibrated confidence and abstention instead of a fixed cosine cut-off |
| [ADR-005](../RAG/adr/ADR-005-single-audit-write.md) | One enriched audit write per policy decision |

## Writing a new record

Copy the headings above, take the next number, and link the record from this table. Keep it to one decision. If a later decision replaces an earlier one, mark the old record **Superseded by NNNN** rather than editing its reasoning.

Related: [ARCHITECTURE.md](../ARCHITECTURE.md) · [technical design](../design/technical-design.md) · [security model](../design/security-model.md)
