# Atlas AI-Evaluation Harness

How Atlas measures its AI outputs, for engineers who want to run the evaluations or check
how a published number was produced. It is a **statistical evaluation harness**, not a
unit-test suite: every AI output is scored against a versioned, labeled **golden
dataset** and treated as a **classification or ranking problem**, reported per metric
and per field/class, never as one hidden average. The canonical RAG numbers are in
[`docs/RAG/metrics/FINAL_METRICS_SUMMARY.md`](../docs/RAG/metrics/FINAL_METRICS_SUMMARY.md);
the overall test strategy is in [`docs/quality/TESTING.md`](../docs/quality/TESTING.md).

- **M-A1** — receipt field **extraction** (per-field P/R/F1)
- **M-A2** — **smart-match** linkage (receipt ↔ card-txn ↔ itinerary segment)
- **M-A3** — **policy-decision** correctness (the hard no-silent-pass gate)
- **RAG (single-policy)** — grounded policy retrieval (recall@k / hit@1 + org-scoping isolation)
- **RAG corpus** — the multi-document Helios retrieval eval: 197 gold queries (145
  answerable + 52 unanswerable) scored for recall@10, nDCG@5, precision@5-on-answered,
  coverage, abstention and AURC, with committed regression floors

The CI-gated path runs entirely on the **deterministic LOCAL providers**
(`LLM_PROVIDER=local`, `EMBEDDINGS_PROVIDER=local`, `RERANK_PROVIDER=local`) — stable,
free, no network. Separate **manual, keyed** runs (`evals/manual/` and the live
calibration targets) exercise real providers (Gemini via OpenRouter, a hosted `bge-reranker-v2-m3`
cross-encoder) and are **never** part of the CI gate.

---

## Layout

```
evals/
  datasets/                       # versioned golden data (pinned as data files)
    m_a1_extraction.jsonl         #  85 receipt-extraction cases
    m_a2_match.jsonl              #  53 smart-match cases (65 gold linkage tuples)
    m_a3_policy.jsonl             #  66 policy-decision cases
    rag_queries.jsonl             #  27 single-policy RAG retrieval cases
    rag_corpus_queries.jsonl      # 197 multi-document RAG cases (145 answerable + 52 unanswerable)
    checkpoint_queries.jsonl      #   2 reranker near-miss checkpoints (outside the 197)
    policy_rules.json             #   7 rules (mirrors database/seeds/policy_content RULES)
    narrative_judge_calibration.jsonl  # 12 human-labeled narratives (manual judge calibration)
  harness/                        # reusable library (pure; no pytest)
    loaders.py                    #  typed dataset loaders + a self-validation CLI
    normalizers.py                #  money / date / text normalizers (value-level compare)
    metrics.py                    #  confusion matrix, P/R/F1, recall@k, nDCG, AURC, cohen_kappa, report
    policy_decision.py            #  the documented M-A3 3-class mapping (classify())
    runners.py                    #  dataset→input builders + harness-owned stub providers
    snapshot.py                   #  regression-snapshot compare/assert
  thresholds.py                   # documented threshold constants (the assertion bars)
  reports/                        # metrics.json + CSVs (git-ignored output; see reports/README.md)
  snapshots/                      # committed regression snapshots
  calibration/                    # Platt + τ* fitters (DB-backed; write ai_agents/rag/*.json; make calibrate-* / threshold-*)
  manual/                         # @pytest.mark.live keyed probes (NOT in CI)
    test_gemini_quality.py                    # gap-fill quality + narrative judge (Gemini)
    test_rag_corpus_huggingface_live.py       # live cross-encoder retrieval eval (HF or Novita)
    test_rag_corpus_ragas_live.py             # RAG faithfulness @ coverage (Gemini judge)
    test_rag_corpus_embedding_matrix_live.py  # live embedder/prefix matrix
    test_rag_corpus_production_baseline_live.py
    test_rag_latency_profile_live.py          # per-stage latency profiler
    conftest.py                   # re-exports backend fixtures (no redefinition)

backend/tests/eval/               # thin pytest ENTRYPOINTS (covered by testpaths → run in CI)
  conftest.py                     # dataset/rule fixtures (reuse, never redefine, backend conftest)
  test_eval_hermetic.py           # Tier-1 hermetic gate (M-A2, M-A3 hard gate, cohen_kappa unit)
  test_rag_corpus_dataset.py      # Tier-1 hermetic: gold-set integrity (labels point at real units)
  test_eval_db_extraction.py      # Tier-2 DB: M-A1 full pipeline
  test_eval_db_retrieval.py       # Tier-2 DB: single-policy RAG recall@k + org-scoping + M-A3 DB corroboration
  test_hybrid_retrieval_paths.py  # Tier-2 DB: dense / sparse / fused retrieval paths
  test_rag_corpus_eval.py         # Tier-2 DB: multi-document RAG eval + regression floors + report
```

The **library** lives in `evals/` so it is import-reusable; the **pytest
entrypoints** live in `backend/tests/eval/` so they are picked up by the existing
`testpaths` and run in CI. The harness owns its **own** stub providers
(`runners.EvalOcr` / `EvalJsonLlm` / `EvalItinerary` / `StorageStub`) — they live in
`evals/`, never as edits to `services/`.

---

## The two tiers

CI runs the eval in two jobs because some gates are genuinely hermetic and must
bite unconditionally, while others need a real Postgres + pgvector.

### Tier-1 — hermetic gate (`test_eval_hermetic.py`)

No database, no network. Runs in the existing bare-`pytest` CI job, so it gates on
**every** push.

- **M-A2**: drive the pure `smart_match` over each golden case, derive linkage
  tuples `(receipt_id, card_txn_id, segment_id, is_duplicate)`, score TP/FP/FN vs
  gold → assert F1 ≥ `MA2_F1_FLOOR`.
- **M-A3 (THE HARD GATE)**: patch `PolicyEvalTool._load_active_rules` with the
  in-memory fixture rules so the **real** per-rule logic runs with no DB; classify
  each verdict into the 3-class label; build the 3×3 confusion matrix; assert
  **violation-recall == 1.0** and **flag-precision ≥ 0.80**.
- **Decision-log shape**: capture the `DecisionRecord` via a patched
  `write_decision_log` to confirm route / provider / confidence.
- **cohen_kappa unit test**: a hermetic, hand-computed coverage of the
  calibration metric used by the manual judge (so the function is tested without
  network even though its caller is offline in CI).

### Tier-2 — pgvector eval-db job (`test_eval_db_*.py`, `@pytest.mark.db`)

Needs a live Postgres + pgvector (the `eval-db` CI job seeds the
`pgvector/pgvector:pg16` service, runs `alembic upgrade head`, then runs only the
`-m db` eval tier). Self-skips when Postgres is unreachable, so it never runs in
the bare hermetic job.

- **M-A1**: per-field P/R/F1 over the **full real `run_receipt_pipeline`** (OCR
  core → routing @0.70 → gap-fill → location reflection → the receipt-row UPDATE →
  the decision-log write), driven over seeded `Receipt` rows. Only the leaf
  providers are stubbed.
- **RAG**: recall@5 / precision@5 / hit@1 vs `gold_clause_refs` over the real
  `GroundedRetriever`, plus the **org-scoping JOIN** isolation probe (two-org
  seed; zero cross-tenant chunks).
- **M-A3 DB corroboration**: the same per-rule logic through the real org-scoped
  rule-loader + a **persisted `ai_decision_logs`** read-back.
- **RAG corpus** (`test_rag_corpus_eval.py`): seeds the four-document Helios corpus
  (219 units today), runs all 197 gold queries through hybrid retrieval (dense + sparse +
  RRF + the local reranker), and asserts:
  - hybrid recall@10 beats the R0 dense-only baseline (0.726667) and stays at or above
    **0.80** (`test_r1_hybrid_beats_r0_baseline`);
  - the committed floors in `test_r6t4_regression_floors`: nDCG@5 ≥ **0.63**,
    precision@5-on-answered ≥ **0.20**, AURC ≤ **0.09**, answerable coverage == **1.0**,
    unanswerable abstention == **0.019231**, plus per-doc-type unit-recall floors;
  - zero cross-tenant chunks and the "trip to New York" regression (≥ 3 gold units in the
    fused top-25);
  - and writes `evals/reports/metrics.json`, `rag_corpus_metrics_table.csv` and
    `risk_coverage.csv` (`test_emit_corpus_metrics_report`).

  Measured on the local path: recall@10 **0.819031**, nDCG@5 0.658420, precision@5
  0.244138, AURC 0.084096 (208-unit corpus; the 219-unit corpus reproduces recall,
  coverage and abstention exactly). These are **plumbing** numbers that prove the wiring
  and catch regressions; the quality numbers with a real cross-encoder and LLM come from
  the keyed runs below.

The `eval-db` tier is **48 tests** (`pytest backend/tests/eval -m db`).

---

## How to run

Run every command from the **repository root** with the project virtualenv active
(`source .venv/bin/activate`), or call the interpreter directly as
`.venv/bin/python -m pytest …`. The DB tiers expect the disposable eval database
`atlas_test_aieval` on `localhost:5432` (the URL is set by the Makefile targets; see the
[local setup guide](../docs/guides/local-setup.md)).

```bash
# Tier-1 + Tier-2 (full eval suite). Exports the disposable eval DB and pins
# PYTHONHASHSEED=0 so the run matches the CI eval-db environment exactly.
make eval
# == export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval \
#    PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local && pytest backend/tests/eval

# Only the DB-backed tier (mirrors the eval-db CI job):
make eval-db
# == … && pytest backend/tests/eval -m db
```

The local embedder (`services/embeddings_provider/local.py`) hashes tokens with keyed
`blake2b` digests, so its vectors are stable across processes. The Makefile and the CI
`eval-db` job still pin `PYTHONHASHSEED=0` so a local run uses exactly the environment CI
uses.

A successful run writes the **`evals/reports/metrics.json`** artifact (per-metric,
per-field/class P/R/F1, confusion matrices, dataset sizes, the RAG corpus metrics) plus
the two RAG CSVs, and validates the committed regression snapshots. Reports are
gitignored; CI uploads them as the `ai-eval-metrics` artifact
([`reports/README.md`](reports/README.md)).

### Self-validating a dataset

```bash
python -m evals.harness.loaders m_a1 evals/datasets/m_a1_extraction.jsonl
# OK: m_a1 parsed 85 record(s) from …   (names: m_a1 | m_a2 | m_a3 | rag | rag_corpus | policy_rules)
```

---

## Metric definitions + threshold rationale

The assertion bars are documented constants in **`evals/thresholds.py`** — read
that file for the per-floor rationale, the measured value, and the date measured.
Each floor is set to `measured − margin` so it **passes today and bites on
regression** (a floor pinned far below measured is a trivially-true assertion and
is forbidden). The summary:

| Metric | What is scored | Threshold (see `thresholds.py`) |
|---|---|---|
| **M-A1** per field | value-level P/R/F1 via money/date/text normalizers | amount/currency/date ≥ 0.95; expense_type ≥ 0.90; vendor ≥ 0.85; location ≥ 0.80 |
| **M-A1** routing | `processing_path` (deterministic vs agent_gap_fill) exact | accuracy == 1.0 |
| **M-A1** provenance | per-field `deterministic` vs `ai_inferred` exact | accuracy == 1.0 |
| **M-A2** | TP/FP/FN over linkage tuples | F1 ≥ 0.95 |
| **M-A3** | per-class P/R/F1 + 3×3 confusion matrix | **violation-recall == 1.0 (HARD)**; flag-precision ≥ 0.80 |
| **RAG** (single-policy) | recall@5 / hit@1 vs gold clause refs; precision@5 reported | recall@5 ≥ 0.80; hit@1 ≥ 0.70; **cross-tenant chunks == 0 (HARD)** |
| **RAG corpus** (local path) | recall@10, nDCG@5, precision@5-on-answered, AURC, coverage, abstention, per-doc-type unit-recall | recall@10 ≥ 0.80; nDCG@5 ≥ 0.63; precision@5 ≥ 0.20; AURC ≤ 0.09; coverage == 1.0; abstention == 0.019231; faq/howto/policy/reference recall ≥ 0.95/0.95/0.55/0.65 |
| **RAG faithfulness** (keyed only) | statement-level LLM judge over the full policy-agent pipeline | ≥ 0.95 (`RAG_FAITHFULNESS_FLOOR`) with grounded coverage ≥ 0.70 (`RAG_GROUNDED_COVERAGE_FLOOR`); never CI-gated |

Per-field comparison goes through `normalizers.py` so a deterministic field written
as the **raw OCR string** (`"$1,234.50"`, `"1.234,50"`, `"11/15/2024"`) still
scores as a correct *value* — this is what makes the M-A1 metrics non-trivial.
Ambiguous DMY/MDY dates default to **US (MDY)** to match the en-US OCR locale
(documented in `normalizers.normalize_date`).

### M-A3 3-class mapping + HARD GATE

The tool's decision is binary (`in_policy: bool`). The harness derives the
operational 3-class label in **`evals/harness/policy_decision.py`** (`classify()`),
documented and faithful to the tool's skip semantics:

```
violation     = any rule verdict definitively failed
needs_review  = no applicable rule (evaluated_rule_count == 0), OR an applicable
                rule could not decide (a "skip" verdict carries in_policy=True,
                observed={} — treated as needs_review, NOT a silent pass)
in_policy     = every applicable rule evaluated and passed
```

Two gates are asserted on this mapping (Tier-1, hermetic, so they bite
unconditionally):

- **VIOLATION-class recall == 1.0** — *no silent in-policy false-pass*. Every gold
  `violation` MUST be predicted `violation`; a real violation slipping through as
  `in_policy` (or even `needs_review`) is **not** allowed. This is a HARD invariant:
  any value below 1.0 **BLOCKS the phase**. It is not a tunable floor.
- **FLAG-class precision ≥ 0.80** — *bounded false flags*. A "flag" is a predicted
  label in `{violation, needs_review}`; it is correct iff gold is also in that set.
  This floor stops the system from passing the recall gate by trivially flagging
  everything.

Gold labels in `m_a3_policy.jsonl` are authored to match this operational
definition exactly.

---

## Regression-snapshot workflow

Beyond thresholds, the structured outputs are **snapshotted** so a decision change
surfaces as a reviewable diff rather than silently moving an aggregate. Committed
snapshots live in `evals/snapshots/`:

- `hermetic_m_a2_linkages.json` — produced linkage tuples
- `hermetic_m_a3_labels.json` — produced 3-class labels
- `db_m_a1_extracted_fields.json` — resolved extracted fields per case
- `db_rag_retrieved_refs.json` — retrieved clause refs per query (gold-intersection)

On every run the freshly-computed output is compared to the committed snapshot; a
mismatch **fails** the test with a unified diff. Snapshots guard **produced**
output, never gold, so the gate catches behavior changes. To deliberately accept a
change, regenerate the snapshots:

```bash
ATLAS_EVAL_UPDATE=1 make eval        # rewrites snapshots, then they are reviewed in the diff
```

A new snapshot is never auto-created in a normal run — a missing snapshot fails
until `ATLAS_EVAL_UPDATE=1` (or `update=True`) writes it once.

---

## Dataset schemas (golden data)

Datasets are **versioned and pinned as data files**; they cover the corpus —
categories, currencies, date formats, tricky receipts — not just the happy path.
One JSON object per line (JSONL); `#`-prefixed lines and blanks are comments.
`policy_rules.json` is a single JSON array. The authoritative schemas + validation
live in `evals/harness/loaders.py`.

- **m_a1_extraction.jsonl** — `{id, category, currency, tags, ocr{fields,
  field_confidence}, itinerary?, gap_fill_json?, gold{extracted_fields,
  field_provenance, processing_path}}`. Covers airfare/hotel/meals/ground/rail/car;
  USD/EUR/GBP/JPY; ISO/US/EU dates; and tricky cases (low-confidence→gap-fill,
  missing field, location reflection, money/date normalization).
- **m_a2_match.jsonl** — `{id, tags, ingest{receipts, card_txns, segments,
  trip_id, unprocessed_receipts}, gold{matches, unmatched_receipts,
  unmatched_card_txns}}`. Covers dup, segment link, non-match, unmatched-only,
  idempotency, date ±1-day boundary, currency mismatch.
- **m_a3_policy.jsonl** — `{id, tags, item{…}, gold_label ∈ {in_policy, violation,
  needs_review}}`. Covers each rule type, borderline (==cap) both sides,
  silent-pass traps (real violations a weak system would pass), and needs_review
  (unevaluable / missing data).
- **rag_queries.jsonl** — `{id, query, gold_clause_refs[], expect ∈ {grounded,
  silent}, tags}`. Covers ≥1 query per clause, multi-clause, off-topic→silent.
- **policy_rules.json** — copies the real `policy_content.py` RULES (caps/tiers)
  so the harness builds in-memory rules (Tier-1) and seeds them (Tier-2).
- **narrative_judge_calibration.jsonl** — human-labeled narratives for the manual
  judge (see below).

---

## Keyed (manual) runs — RAG

These need real provider keys in the environment (never committed), self-skip cleanly
without them, and are **not** part of CI. Each Makefile target sets the eval database,
the provider selection and `PYTHONHASHSEED=0` where retrieval must stay deterministic.

| Target | What it measures | Needs | Output |
|---|---|---|---|
| `make eval-rerank-live` | 197-query retrieval with the live HuggingFace-hosted `bge-reranker-v2-m3` over local-embedding candidates (+ near-miss checkpoint) | `HF_API_TOKEN` | `reports/rag_corpus_hf_rerank_live.json` |
| `make eval-rerank-live-novita` | the same eval with the identical model hosted on Novita (throttled to ≤ 30 batches/min) | `RERANK_API_KEY` | `reports/rag_corpus_novita_rerank_live.json` |
| `make eval-live-r5` | faithfulness @ coverage over the full policy-agent pipeline (Gemini generation + statement-level Gemini judge, Novita reranker, local embeddings); enforces `RAG_FAITHFULNESS_FLOOR = 0.95` and `RAG_GROUNDED_COVERAGE_FLOOR = 0.70` | `LLM_API_KEY`, `RERANK_API_KEY` | printed summary |
| `make eval-profile-live` | per-stage latency (retrieval / rerank / generation) as min/median/max, plus an `EXPLAIN` of the dense read | `LLM_API_KEY`, `RERANK_API_KEY` | printed summary |
| `make eval-live-matrix` | live embedder × contextual-prefix matrix (OpenRouter embeddings) | `LLM_API_KEY` (reused for embeddings), `RERANK_API_KEY` | `reports/rag_corpus_embedding_matrix.json` |
| `make recalibrate` / `calibrate-*` / `threshold-*` / `recalibrate-live` | re-fit the Platt parameters and τ\* | DB (+ keys for the live variants) | `ai_agents/rag/rerank_calibration.json`, `abstention_threshold.json` (live variants write `*.live.json` scratch files) |

Latest keyed results (all on the 208-unit corpus): recall@10 **0.959** with the live
cross-encoder; faithfulness **0.981 @ 74% grounded** (108/145, C7R run, 2026-11-11; the
first R6 measurement was 0.991 @ 78%, 113/145). Sources and conditions:
[FINAL_METRICS_SUMMARY](../docs/RAG/metrics/FINAL_METRICS_SUMMARY.md).

---

## Manual Gemini eval (receipts and narratives)

`evals/manual/test_gemini_quality.py` is marked **`@pytest.mark.live`** (module-level
`pytestmark`) so it is **SKIPPED by default and never runs in the CI gate**. It
targets the **real** provider via `LLM_PROVIDER=openrouter` + `LLM_API_KEY` (the
OpenRouter → Gemini adapter). Every live test self-skips cleanly when
`LLM_API_KEY` is unset, and the module imports with no network.

### Run command

From the repository root (`make eval-live` runs `LLM_PROVIDER=openrouter pytest evals/manual -m live`):

```bash
LLM_PROVIDER=openrouter LLM_API_KEY=... \
  .venv/bin/python -m pytest evals/manual/test_gemini_quality.py -m live -p no:cacheprovider -v
```

The gap-fill measurement additionally needs the eval DB and the hash seed (it
drives the real receipt pipeline over seeded rows):

```bash
LLM_PROVIDER=openrouter LLM_API_KEY=... \
  DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval \
  PYTHONHASHSEED=0 \
  .venv/bin/python -m pytest evals/manual/test_gemini_quality.py -m live -p no:cacheprovider -v
```

> Note: `evals/manual/conftest.py` re-exports the backend `db_session` / `org_a` /
> `users_a` / `m_a1_cases` fixtures (and the autouse `_schema` migration) so the
> manual suite reuses them verbatim. Because `_schema` is autouse + session-scoped,
> a live run runs the alembic migration once against the **disposable** eval DB
> even for the narrative-only test.

### What it measures

1. **M-A1 gap-fill quality.** Re-runs the gap-fill subset (cases tagged
   `agent_gap_fill` / `low-confidence`) through `run_receipt_pipeline`, swapping the
   harness JSON-LLM stub for the **real** `get_llm_provider()` so actual Gemini
   infers the low-confidence fields. OCR / itinerary / storage stay the
   deterministic harness stubs, so the experiment isolates the LLM's inference
   quality. Scores the **gap-filled fields only** (gold provenance == `ai_inferred`)
   vs gold using the same normalizers/metrics as the CI eval, and reports per-field
   P/R/F1.

2. **Narrative faithfulness via LLM-as-judge.** For the calibration narratives,
   asks a Gemini judge two **separate** yes/no questions — **faithfulness** (is
   every claim supported by the source facts?) and **relevance** (does it actually
   summarize *this* report?) — and reports the two scores separately. The two axes
   are never collapsed: a fluent on-topic narrative can still hallucinate, and a
   strictly-true narrative can still be irrelevant filler (both appear in the
   calibration set).

### Judge calibration + Cohen's κ

LLM-as-judge is reserved for free-text narrative faithfulness and runs **only** in
this manual (non-CI) path. To know whether the judge can be trusted, it is
**calibrated** against the human labels in
`evals/datasets/narrative_judge_calibration.jsonl`: the harness computes
**Cohen's kappa** (`metrics.cohen_kappa`) between the human labels and the judge's
labels — **separately for faithfulness and relevance**.

Cohen's κ is chance-corrected agreement,
`κ = (p_observed − p_chance) / (1 − p_chance)`. Interpretation (Landis & Koch):

| κ | agreement |
|---|---|
| < 0.0 | poor (worse than chance) |
| 0.00 – 0.20 | slight |
| 0.21 – 0.40 | fair |
| 0.41 – 0.60 | moderate |
| 0.61 – 0.80 | substantial |
| 0.81 – 1.00 | almost perfect |

**A judge with κ below ~0.4 vs the human labels is not trustworthy** — its
narrative scores should be treated as *advisory only*, not as a quality gate. The
`cohen_kappa` function itself is covered by a hermetic, hand-computed unit test in
`test_eval_hermetic.py` (so the math is verified without network even though the
judge it scores is offline in CI).

The manual tests assert only the loosest sanity bars (the judge must beat a coin
flip; the live gap-fill must resolve some field). They are **diagnostic**, not part
of any pass/fail gate — a flaky live run must never masquerade as a hard
pass/fail of the harness.
