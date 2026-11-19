# Phase 7 · P7-T3 — AI Output Validation (eval harness)

**Date:** 2026-06-20 · **Branch:** `test/ai-eval` · **Status:** COMPLETE

> This is the canonical phase log for P7-T3. P7-T3a (OpenRouter/Gemini adapter, already on `main`)
> and P7-T3b (this harness) are both covered here; the file supersedes the draft
> `docs/phase_logs/P7-T3b_ai_eval.md`.

---

## Goal

Phase 7's AI-accuracy task builds a *labeled evaluation set* — not unit tests — that measures the
three PRD Expense-Accuracy metrics against a versioned golden dataset and asserts
precision/recall/F1 thresholds in CI on the **deterministic local provider** (free, no network):

- **M-A1** — receipt field extraction (per-field correctness over the real `run_receipt_pipeline`).
- **M-A2** — smart-match linkage (receipt ↔ card_txn ↔ itinerary segment).
- **M-A3** — policy-decision correctness. "No silent in-policy false-pass" is encoded as
  **violation-class recall == 1.0** (a HARD GATE that BLOCKS if < 1.0); "bounded false flags" as a
  **flag-class precision floor ≥ 0.80**.
- **RAG** — grounded retrieval Recall@k / Hit@1 / Precision@k against ground-truth clause refs,
  plus a HARD **org-scoping** invariant (any cross-tenant chunk is both a data leak and a failure).

This is a classification problem, reported per field/class — never a single hidden average
([PRD.md §Expense Accuracy]; [TECHNICAL_DESIGN.md:226-227] — "the decision log doubles as the
dataset for measuring … M-A1–3"). The CI-gated path is `LLM_PROVIDER=local` only. The
OpenRouter/Gemini adapter (P7-T3a) is used **only** in the separate manual `eval-live` run — it
never touches the CI gate.

---

## What was built

### Harness library — `evals/` (repo root)

**`evals/datasets/`** — the versioned golden corpus (JSONL, one labeled case per line) plus
`policy_rules.json`:

- `m_a1_extraction.jsonl` — **85** cases.
- `m_a2_match.jsonl` — **53** cases (→ **65** gold linkage tuples).
- `m_a3_policy.jsonl` — **66** cases (33 in_policy / 26 violation / 7 needs_review; 14 silent-pass
  traps).
- `rag_queries.jsonl` — **27** cases (22 grounded / 5 silent).
- `narrative_judge_calibration.jsonl` — **12** human-labeled cases for LLM-as-judge calibration.
- `policy_rules.json` — the **7** active seed rules copied verbatim from
  `database/seeds/policy_content.py` (AIR-CABIN, AIR-PREF, HOTEL-CAP, HOTEL-PREF, MEALS-PD,
  GROUND-CLASS, RECEIPT-REQ); used to build in-memory `PolicyRule(status=active)` rows in Tier-1
  and to corroborate the seeded rules in Tier-2.

**`evals/harness/`** — the reusable, production-free library:

- `loaders.py` — JSONL/JSON case loaders with unique-id enforcement.
- `normalizers.py` — money (`"$1,234.50"` / EU `"1.234,50"` → `Decimal` + currency code), date
  (US-MDY / EU-DMY / ISO → `YYYY-MM-DD`), and text (casefold/strip) normalizers so a correct value
  in a different surface form is not scored as a miss.
- `metrics.py` — confusion matrix, per-class / per-field precision/recall/F1, recall@k /
  precision@k / hit@k, the `metrics.json` report writer, and Cohen's κ for LLM-as-judge calibration.
- `policy_decision.py` — the documented **M-A3 3-class mapping** (`classify(verdict)`):
  - any failing `RuleVerdict` → `"violation"`;
  - `evaluated_rule_count == 0` → `"needs_review"`;
  - any applicable rule returning a skip verdict (`in_policy=True, observed={}`) → `"needs_review"`;
  - else → `"in_policy"`.
  This treats an unevaluable rule as needs_review rather than a silent pass — faithful to the tool,
  conservative on safety. The skip-verdict default at [policy_eval.py:623-633] cannot silently pass.
- `runners.py` — the harness's **own** deterministic stub providers (`_EvalOcr`, `_EvalJsonLlm`,
  `_EvalItinerary`, `_StorageStub`) — they live in `evals/`, **not** as edits to `services/`. The
  canned `LocalOcrProvider` knows only 3 hints and `LocalLlmProvider` returns non-JSON (real
  gap-fill can't parse); the stubs return each case's authored OCR payload / gap-fill JSON.
- `snapshot.py` — canonical-JSON regression compare: serialize per-case structured outputs, diff
  against the committed baseline, fail on drift. Updated via `ATLAS_EVAL_UPDATE=1`.

**`evals/thresholds.py`** — pure threshold constants with measured-value + date comment beside each
floor and the rationale for why it bites.

**`evals/snapshots/`** (committed baseline) — `hermetic_m_a2_linkages.json`,
`hermetic_m_a3_labels.json`, `db_m_a1_extracted_fields.json`, `db_rag_retrieved_refs.json`.

**`evals/reports/metrics.json`** — emitted CI artifact (gitignored; regenerated each run).

**`evals/README.md`** — full harness documentation: run commands, marker semantics, snapshot update
flow, manual Gemini hook, and dataset authoring guide.

### Pytest entrypoints — `backend/tests/eval/`

- `test_eval_hermetic.py` (Tier-1, **10 tests**, NO DB) — M-A2 over real `smart_match`; M-A3 over
  the real `PolicyEvalTool` against fixture rules; the **violation-recall HARD GATE** + flag-precision
  floor; a captured `DecisionRecord`; the `metrics.json` writer; M-A2/M-A3 snapshot guards; Cohen's-κ
  unit tests.
- `test_eval_db_extraction.py` (Tier-2, `@pytest.mark.db`, **17 tests**) — full M-A1 over the real
  `run_receipt_pipeline` with harness stubs over `make_receipt` rows; per-field P/R/F1;
  processing_path + provenance accuracy; structural gate (both routing paths ≥ 5 cases each);
  M-A1 snapshot guard.
- `test_eval_db_retrieval.py` (Tier-2, `@pytest.mark.db`, **11 tests**) — M-A3 corroboration via
  real org-scoped `evaluate()` + **read-back of persisted `ai_decision_logs`** keyed by
  `input_summary["ref_id"]`; RAG recall@k / precision@k / hit@1; the **org-scoping JOIN** two-org
  leak test; RAG snapshot guard.
- `conftest.py` — thin glue; does **not** redefine `db_session` / `local_ai` / factories.

### Manual hook — `evals/manual/`

- `test_gemini_quality.py` (`@pytest.mark.live`) — re-runs M-A1 gap-fill quality through real Gemini
  and scores narrative faithfulness via LLM-as-judge, calibrated against the 12-case human set with
  Cohen's κ. Self-skips cleanly when `LLM_API_KEY` is absent. NOT in CI.
- `conftest.py` — imports + re-binds fixture callables from `backend.tests.conftest` by name (avoids
  the pytest-8 `pytest_plugins` non-top-level error).

### Wiring (harness plumbing; not production code)

- `pyproject.toml` — `evals` added to ruff `known-first-party` and to pytest `testpaths`.
- `Makefile` — `eval` (full suite), `eval-db` (`-m db`, mirrors the CI job, exports eval-DB URL +
  `PYTHONHASHSEED=0`), `eval-live` (manual Gemini, `-m live`, scoped to `evals/manual`); mypy
  extended to include `evals`.
- `.github/workflows/ci.yml` — new **`eval-db`** job: `pgvector/pgvector:pg16` service → inherited
  `_schema` autouse fixture migrates the disposable `atlas_test_aieval` DB → `pytest
  backend/tests/eval -m db` on `LLM_PROVIDER=local` + `PYTHONHASHSEED=0` → uploads
  `evals/reports/metrics.json` as the `ai-eval-metrics` artifact (`if: always()`). The CI `backend`
  job mypy step was also extended to `mypy backend config evals`.
- `.gitignore` — ignores `evals/reports/` only (the snapshots baseline stays committed).

---

## Harness home + rationale (two tiers)

The dataset + reusable library live in **`evals/` at the repo root**; the **thin pytest entrypoints
live under `backend/tests/eval/`** (already under `testpaths`, so they collect under bare `pytest`
without touching the CI config for Tier-1).

The split is driven by a real CI constraint: the existing `backend` CI job runs bare `pytest` with
**no Postgres service**, so every `db_session` / `@pytest.mark.db` test silently **skips** in CI.
The M-A3 no-silent-pass HARD GATE must bite **unconditionally**, so it runs **Tier-1 hermetic** (no
DB) inside the existing `backend` job. The full receipt pipeline, the real org-scoping JOIN, the
persisted-log read, and pgvector RAG genuinely need a database, so they run **Tier-2** in the new
`eval-db` pgvector job. The Tier-1 hermetic tests also collect in the `backend` job (their DB-marked
siblings self-skip there), so `eval-db` deliberately scopes to `-m db` and does not duplicate Tier-1.

This is the **approved deviation** from the P1-T5 hermetic-CI mandate, appropriate for Phase 7.

---

## Reconcile (spec/plan assumptions vs. codebase)

- **M-A1** — `run_receipt_pipeline(session, *, context, receipt_id, ocr, llm, storage,
  itinerary_tool=None) -> Receipt` [ai_agents/receipt_pipeline/pipeline.py:63]. Route threshold
  **0.70** (field kept deterministic if conf ≥ 0.70, else gap-filled). ⚠️ `LocalLlmProvider` returns
  **non-JSON** and `LocalOcrProvider` is canned for 3 hints → harness injects its own stubs.
- **M-A2** — `smart_match(data: IngestedData) -> list[MatchCandidate]`
  [ai_agents/agents/expense_agent/matching.py:112]. Pure, hermetic, no DB.
- **M-A3** — `PolicyEvalTool().evaluate(item, *, context, session) -> PolicyVerdict`
  [ai_agents/tools/policy_eval.py:128]. Rules loaded via the **org-scoping JOIN**
  [policy_eval.py:205-214]. ⚠️ skip verdict defaults `in_policy=True, observed={}` [policy_eval.py:623]
  — the harness maps this to `needs_review`, not a silent pass.
- **RAG** — org-scoping JOIN at policy_retrieval.py:107-109. Local embedder is a salted `hash()` →
  stable only within one process; `PYTHONHASHSEED=0` pinned to lock it across all eval runs.
- **Seed** — seeds set AI outputs (`extracted_fields`, `in_policy`, …) to **NULL** → every golden
  case carries its own gold; no "canned == gold" trivial assertion.

---

## Dataset (sizes + coverage)

| Metric | Cases | Coverage highlights |
|--------|-------|---------------------|
| M-A1   | **85** | Categories airfare/meals/lodging/ground/rail/car; currencies USD/EUR/GBP/JPY; date formats ISO/US-MDY/EU; tricky = low-confidence→gap-fill (42 `agent_gap_fill`, 43 `deterministic`), EU money norm, location-reflection, missing field. |
| M-A2   | **53** (65 tuples) | Clean match, duplicate, non-match (amount/currency/date mismatch must NOT link), unmatched-only, idempotency, date ±1-day boundary. |
| M-A3   | **66** | One+ per rule_type; borderline (== cap) both sides; **14 silent-pass traps** (real violations); `needs_review` = unevaluable (missing data / no applicable rule). |
| RAG    | **27** | One+ per clause; multi-clause; off-topic → silent; two-org pair for cross-tenant probe. |
| Judge-cal | **12** | Human-labeled narratives for Cohen's κ calibration of LLM-as-judge. |
| Policy rules | **7** | Byte-faithful copy of `policy_content.py RULES`. |
| **Total** | **231** | — |

---

## Metrics (MEASURED on local provider, from `evals/reports/metrics.json`)

Floors are set just below measured so they pass now and bite on regression.

### M-A1 — per-field (Tier-2, full real pipeline, 85 cases)

| Field | P | R | F1 | Floor |
|-------|---|---|----|-------|
| amount       | 1.0 | 1.0 | 1.0 | recall ≥ 0.95 |
| currency     | 1.0 | 1.0 | 1.0 | recall ≥ 0.95 |
| date         | 1.0 | 1.0 | 1.0 | recall ≥ 0.95 |
| expense_type | 1.0 | 1.0 | 1.0 | recall ≥ 0.90 |
| vendor       | 1.0 | 1.0 | 1.0 (sup 84) | recall ≥ 0.85 |
| location     | 1.0 | 1.0 | 1.0 (sup 82) | recall ≥ 0.80 |

`processing_path` accuracy = **85/85 = 1.0** (floor 1.0). `field_provenance` accuracy =
**510/510 = 1.0** (floor 1.0). Each field is asserted SEPARATELY.

### M-A2 — smart-match (Tier-1 hermetic, 53 cases, 65 tuples)

P = 1.0, R = 1.0, **F1 = 1.0**. Floor **F1 ≥ 0.95** (raised from PRD starting bar of 0.90;
a single FP/FN on 65 tuples drops F1 to ~0.985 — still above the floor — so it takes a real
regression of a handful of links to trip it, which is the right sensitivity).

### M-A3 — policy-decision (Tier-1 hermetic, 66 cases, 3-class)

- Per-class P/R/F1 all **1.0** (in_policy support 33, needs_review 7, violation 26).
- Confusion matrix perfectly diagonal; 0 off-diagonal.
- **VIOLATION-class recall = 1.0 → HARD GATE MET.** Not a tunable floor.
- **flag-class precision = 1.0 ≥ floor 0.80.**
- Tier-2 corroboration (real org-scoped JOIN, 66 cases): 0 mismatches vs Tier-1; **2** persisted
  `ai_decision_logs` rows read back and matched by `input_summary["ref_id"]`.

### RAG — retrieval + org-scoping (Tier-2, 27 queries)

- **Recall@5 = 1.0** ≥ floor 0.80.
- **Hit@1 = 1.0** ≥ floor 0.70.
- **Precision@5 = 0.584** — reported, not gated (structurally bounded with ~8 total chunks).
- **Cross-tenant chunks = 0** (HARD; org_b has 8 retrievable embeddings; org-A retrieval surfaces 0).
  The JOIN at policy_retrieval.py:107-109 is the only enforcement — an unscoped ANN would surface all 8.

---

## Self-verification

1. **Tier-1 green:** `pytest backend/tests/eval/test_eval_hermetic.py` → **10 passed** in 0.24s.
2. **Hermetic with dead DATABASE_URL:** Tier-1 passes even with an unreachable Postgres — proves the
   HARD GATE bites unconditionally in the bare CI job.
3. **Tier-2 green:** full eval suite → **38 passed** (10 hermetic + 17 + 11) with the eval-DB +
   `PYTHONHASHSEED=0`.
4. **Lint/type:** `ruff check .` → all checks passed; `mypy backend config evals` → no issues found
   in 127 source files.
5. **Gates-bite probes (all reverted byte-identical, sha256 verified):**
   - (a) Forced real VIOLATION → predicted in_policy → violation-recall 0.9615 → **HARD GATE FAILS**. ✔
   - (b) Perturbed M-A1 gold amount → per-field P/R 0.9412 < 0.95 floor. ✔
   - (c) Swapped RAG gold clause_ref → hit@1 0.5455 / recall@5 0.7273, both below floors. ✔
6. **Snapshot guards:** compare **produced** output to the committed baseline — a drift fails with a
   unified diff and requires a deliberate `ATLAS_EVAL_UPDATE=1` update. Never auto-passes.

---

## QA findings

1. **(FIXED — dataset-only)** Two M-A1 cases had gold location authored as the raw gap-fill value
   rather than the reflection-grounded value; re-labelled to the pipeline-correct result.
2. **(FIXED — dataset-only)** Several EU-money and EU-date surface-form cases scored as misses until
   the normalizer was applied to the gold side; corrected.
3. **(NO PRODUCTION DEFECTS)** The eval found **zero production defects** in `run_receipt_pipeline`,
   `smart_match`, `PolicyEvalTool.evaluate`, its org-scoping JOIN, `write_decision_log`,
   `PolicyRetrievalTool`, or `GroundedRetriever`. Had a defect surfaced it would have been
   STOPPED + FLAGGED with file:line (per the harness fences), never fixed.

---

## Manual Gemini hook (NOT in CI)

The CI gate is `LLM_PROVIDER=local` only — deterministic, free, no network. A separate manual run
re-measures model-dependent quality:

```bash
export LLM_PROVIDER=openrouter
export LLM_API_KEY=<key>            # OpenRouter key; LLM_MODEL defaults to google/gemini-2.5-flash
make eval-live                      # == pytest evals/manual -m live -p no:cacheprovider -v
```

Scope: re-run M-A1 **gap-fill quality** (real Gemini infers missing fields vs gold) + narrative
**faithfulness** (LLM-as-judge, calibrated against the 12-case human-labeled set; Cohen's κ reported;
faithfulness and relevance are reported separately). Self-skips cleanly when `LLM_API_KEY` is absent.

---

## CI wiring

- **`eval-db`** job in `.github/workflows/ci.yml`: provisions `pgvector/pgvector:pg16` → the
  inherited `_schema` autouse fixture runs `DROP SCHEMA CASCADE` + `alembic upgrade head` +
  `CREATE EXTENSION vector` on the disposable `atlas_test_aieval` DB → `pytest backend/tests/eval
  -m db` on `LLM_PROVIDER=local` + `PYTHONHASHSEED=0` → uploads `evals/reports/metrics.json` as
  `ai-eval-metrics` artifact (`if: always()`).
- The **Tier-1 HARD GATE** runs inside the existing hermetic `backend` job — a violation-recall
  regression blocks the build even with no database present.
- No live LLM / network in any CI path. Gemini is exercised only by the manual `eval-live` run.
- Deviation from P1-T5 hermetic mandate: the `eval-db` pgvector job is the approved exception for
  Phase 7 (real DB-backed eval is required for the M-A1 full pipeline and org-scoping probe).

---

## Files changed

**Created:** `evals/__init__.py`, `evals/thresholds.py`, `evals/README.md`,
`evals/datasets/{m_a1_extraction,m_a2_match,m_a3_policy,rag_queries}.jsonl`,
`evals/datasets/narrative_judge_calibration.jsonl`, `evals/datasets/policy_rules.json`,
`evals/harness/{__init__,loaders,normalizers,metrics,policy_decision,runners,snapshot}.py`,
`evals/snapshots/{hermetic_m_a2_linkages,hermetic_m_a3_labels,db_m_a1_extracted_fields,
db_rag_retrieved_refs}.json`,
`evals/manual/{__init__,conftest,test_gemini_quality}.py`,
`backend/tests/eval/{__init__,conftest,test_eval_hermetic,test_eval_db_extraction,test_eval_db_retrieval}.py`,
`docs/phase_logs/P7-T3-ai-output-validation.md`.

**Modified:** `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `.gitignore`.

**Generated (gitignored):** `evals/reports/metrics.json`.

**No production code modified.** `ai_agents/`, `services/`, `backend/app/`, `database/`, `config/`
are read-only.
