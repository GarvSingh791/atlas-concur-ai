# Evaluation reports — `evals/reports/`

What the evaluation harness writes here and what CI does and does not gate — for anyone
looking for a metrics file that is not in the repository. Everything here **except this
README is gitignored**: the files are regenerated on every `make eval-db` / CI run, and CI
uploads `metrics.json` and the two CSVs as the `ai-eval-metrics` GitHub Actions artifact
(3-day retention; see `.github/workflows/ci.yml`). The canonical, sourced numbers are in
[`docs/RAG/metrics/FINAL_METRICS_SUMMARY.md`](../../docs/RAG/metrics/FINAL_METRICS_SUMMARY.md).

## Artifacts

| File | Emitted by | Contents |
|------|-----------|----------|
| `metrics.json` | `test_emit_corpus_metrics_report` | Full nested metrics: overall + per-query-type + per-doc-type (recall/nDCG/precision at k=10 and k=5), coverage, abstention, AURC, risk-coverage points, faithfulness proxy, latency. |
| `rag_corpus_metrics_table.csv` | `test_emit_corpus_metrics_report` (R6-T4) | Flat OVERALL + PER-DOC-TYPE table: `doc_type, n_answered, recall_at_10, ndcg_at_5, precision_on_answered, coverage, abstention, aurc`. `n_answered` makes a small-support cell (e.g. `faq` recall `1.000` at `n=1`) legible as low-support, not signal. `abstention`/`aurc` are `N/A` per doc type (the unanswerable set is out-of-corpus / untyped). |
| `risk_coverage.csv` | `test_emit_corpus_metrics_report` (R6-T4) | The risk-coverage curve as a τ-sweep: `tau, coverage, risk`. **No PNG** is rendered — the project has no plotting dependency and R6-T4 does not add one; the curve DATA is the machine-readable artifact. AURC (its area) is in the table + `metrics.json`. |
| `rag_corpus_hf_rerank_live.json` / `rag_corpus_novita_rerank_live.json` | `make eval-rerank-live` / `make eval-rerank-live-novita` (keyed, manual) | Live cross-encoder retrieval metrics at k=10 and k=5 next to the RRF baseline, rerank latency, fail-safe fallback count. Not produced by CI. |
| `rag_corpus_embedding_matrix.json` | `make eval-live-matrix` (keyed, manual) | Live embedder × contextual-prefix matrix (cells C0–C3). Not produced by CI. |

## What CI gates — and what it deliberately does NOT

The deterministic CI build runs `LLM_PROVIDER=local` (`make eval-db`). It has **no LLM judge**, so
it structurally CANNOT gate faithfulness. The honest split:

- **CI (deterministic, local-path) gates** — committed floors in `evals/thresholds.py`, enforced by
  `test_r6t4_regression_floors`: `recall@10 ≥ 0.80`, `nDCG@5 ≥ 0.63`, `precision-on-answered ≥ 0.20`,
  `AURC ≤ 0.09`, `coverage == 1.0`, `abstention == 0.019231`, plus the per-doc-type unit-recall
  floors. A drop across any of these FAILS CI with a clear message.
- These lock the **local-path plumbing** (blake2b embedder + cosine gate + RRF-anchored local
  reranker — identity-on-gold by design), which is byte-reproducible. They are **NOT** the production
  calibrated numbers (τ\* = 0.039299, fitted on the keyed HuggingFace-hosted cross-encoder run and
  carried forward to the identical Novita-hosted model used by the keyed runs). The local
  `recall@10 = 0.819031` is the plumbing number, **not** a production faithfulness/quality figure.
- **Faithfulness is gated ONLY on the keyed `make eval-live-r5` run** (run by hand with
  `LLM_PROVIDER=openrouter` + keys; there is no CI workflow for it) via
  `thresholds.RAG_FAITHFULNESS_FLOOR = 0.95` in `evals/manual/test_rag_corpus_ragas_live.py`. The
  same run fails below `thresholds.RAG_GROUNDED_COVERAGE_FLOOR = 0.70` grounded coverage. It
  self-skips without keys. Latest keyed measurement: **0.981 faithful @ 74% grounded** (108/145,
  C7R run, 2026-11-11); first measurement at R6: 0.991 @ 78% (113/145). That run prints its
  summary rather than writing a file here.

CI never claims to gate faithfulness.
