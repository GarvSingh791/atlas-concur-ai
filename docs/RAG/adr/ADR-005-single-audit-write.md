# ADR-005 — One enriched audit write per decision

**Status:** Accepted · implemented across R4–R6 · shipped (runs on every deployment, including public staging)
**Decision owner:** Garv Singh Saini (sole author)
**Related:** [RAG_ARCHITECTURE §Audit logging](../RAG_ARCHITECTURE.md#audit-logging) · [audit diagram](../diagrams/audit_pipeline.mmd)

## Context

Every policy answer is an auditable enterprise decision: what was asked, what was
retrieved, why it was (or wasn't) answered, and with what confidence. The system
needs a durable, tenant-scoped record for each answered or abstained query. The
question is *how* that record is written — incrementally as the pipeline runs, or
once at the end.

## Options considered

1. **Multiple incremental writes** (one row as retrieval finishes, another after
   reranking, another after generation). Rich streaming trace, but: multiple rows per
   logical decision to correlate, partial/orphan rows when a later stage fails, more
   DB round-trips on the hot path, and a reconciliation burden for anyone reading the
   log.
2. **Fire-and-forget logging** (best-effort, off-transaction). Cheap, but a decision
   can be presented to a user yet missing from the audit trail — unacceptable for an
   enterprise policy system.
3. **One enriched write per decision**, inside the request transaction, carrying the
   full pipeline trace in structured fields.

## Decision

Write **exactly one** `ai_decision_logs` row per Q&A invocation, via a single
`write_decision_log(...)` call on whichever branch is taken (grounded *or* silent —
they are mutually exclusive). The pipeline threads its evidence up an `audit_trail`
and serializes it into that one row:

- top-level: `agent`, `task_type`, `route`, `model_provider`, `correlation_id`,
  `confidence` (quantized `NUMERIC(4,3)`), `reflection_outcome`, `latency_ms`;
- `tools_used["GroundedRetriever"]`: the reranked candidates
  (`document_id`, `unit_id`, `fused_rank`, `rerank_score`) and the **gate**
  (`source` cosine|calibrated, `signal`, `threshold`, `tau_star`);
- `output_summary`: the document-qualified citations and the NLI result.

All JSONB fields pass through PII-minimization before the write; `flush()`-only, so
the caller's transaction owns the commit.

## Consequences

**Positive**
- One row = one decision: no correlation of partial rows, no orphans, and the full
  "why this answer" (gate signal, τ\*, candidate ranks, citations) is reconstructable
  from a single record.
- Atomic with the request transaction — a presented answer is always logged, and a
  rolled-back request logs nothing.
- One write on the hot path instead of several.

**Negative / tradeoffs**
- No intermediate/streaming trace of each stage as it happens (accepted — the
  enriched final row captures the same evidence, and per-stage timing lives in the
  latency profiler instead).
- The row's JSONB payload is richer and must be kept schema-stable; per-candidate
  `calibrated_score` is intentionally *not* stored per-row (only the top chunk's
  calibrated score, as `gate.signal`) to keep the payload bounded.

## Research reference

Enterprise auditability / human-on-the-loop patterns in companion report
[`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md) §3.2–3.3 (how publicly
documented assistants such as Ramp and Glean surface traceable, reviewable AI decisions,
from public product material only).
