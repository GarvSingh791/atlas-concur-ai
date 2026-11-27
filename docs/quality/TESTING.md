# Testing and evaluation

How Atlas is tested, what each test tier proves, how to run it, and the current results.
Written for reviewers who want to know whether the numbers in the [README](../../README.md)
can be trusted, and for engineers who want to reproduce them.

**In one screen:** six test tiers plus static analysis run without API keys, because every
external provider has a deterministic `local` implementation; a seventh, keyed tier needs real
models and is run by hand. CI runs seven jobs and only deploys a commit that passed all of
them. AI quality is measured as classification metrics against versioned gold datasets, and
the RAG engine's keyed results are reported apart from the CI numbers.

| | |
|---|---|
| Hermetic pytest (no database) | **4225 passed**, 4 skipped |
| DB-backed pytest | **2069 passed**, 8 skipped |
| AI-eval DB tier (`eval-db`) | **48 passed**; RAG recall@10 0.819 on the deterministic path |
| Vitest (frontend unit) | **968 tests** in 133 files |
| API contract matrix | **80** `/api/v1` endpoints |
| Playwright e2e | employee, manager and finance flows, cross-role handoff, RBAC in the UI |
| CI | **7 jobs**: backend, frontend, eval-db, e2e, docker-build, secret-scan, deploy-staging |

Pass counts are from the phase 12 merge gate (PR #14) on an isolated database with all
providers `local`; the README's [Quality & Evaluation](../../README.md#quality--evaluation)
section is the canonical copy.

---

## Contents

- [Test pyramid](#test-pyramid)
- [The tiers](#the-tiers)
- [How to run each tier](#how-to-run-each-tier)
- [CI job graph](#ci-job-graph)
- [AI accuracy evaluation](#ai-accuracy-evaluation)
- [RAG metrics](#rag-metrics)
- [Determinism strategy](#determinism-strategy)
- [The R6 story: a green CI that hid a broken live path](#the-r6-story-a-green-ci-that-hid-a-broken-live-path)
- [Known test caveats](#known-test-caveats)

---

## Test pyramid

The base is wide and fast; the top is narrow, slow and, for the keyed evaluations, outside CI.

```mermaid
flowchart TB
  K["Keyed / live evals (manual, needs API keys)<br/>faithfulness, live cross-encoder, latency profile"]
  E["Playwright e2e (CI: e2e)<br/>real UI per role against a seeded backend"]
  A["AI-eval DB tier (CI: eval-db)<br/>48 tests: M-A1, RAG recall, tenant isolation, regression floors"]
  C["API contract matrix (inside the DB suite)<br/>80 endpoints: 401, 403, error envelope"]
  D["DB-backed pytest (-m db)<br/>2069 passed: real Postgres + pgvector, migrations, scoping"]
  H["Hermetic pytest + Vitest (CI: backend, frontend)<br/>4225 passed + 968 Vitest: units, M-A2/M-A3 gates, components"]
  S["Static analysis<br/>ruff, mypy, ESLint, Prettier, tsc, secret scan, env audit"]
  K --> E --> A --> C --> D --> H --> S
```

---

## The tiers

| Tier | Where it lives | What it proves | Runs in CI |
|---|---|---|---|
| **Hermetic pytest** | `backend/tests`, `ai_agents/tests`, `services/tests`, `evals` (pytest `testpaths`) | Business rules, state machines, policy evaluation, agent logic, provider adapters, settings validation, rate limiter, upload checks, and the hermetic AI gates (M-A2, M-A3) with no database or network. DB-marked tests skip themselves | `backend` job (`pytest`) |
| **DB-backed pytest** (`-m db`) | same paths, `@pytest.mark.db` | Real Postgres + pgvector behaviour: every Alembic migration, record-level scoping per role, tenant isolation, idempotency, concurrency, audit rows, decision-log writes, lifecycle flows end to end through the API | Partly: the `eval-db` subset in CI; the full suite runs locally at each merge gate |
| **API contract matrix** | `backend/tests/api/test_contract_matrix.py`, registry in `backend/tests/api/endpoints.py` | The registry equals the live route table (`assert len(registry) == len(live) == 80`); every protected endpoint answers 401 without a token, 403 for a forbidden role, and always with the standard error envelope, never a 500 | The route-coverage check is hermetic (`backend` job); the 401/403 checks are `db`-marked and run with the DB suite |
| **AI-eval DB tier** (`eval-db`) | `backend/tests/eval/` (entry points), `evals/` (harness, datasets, thresholds) | M-A1 extraction through the real receipt pipeline, RAG recall and abstention on the 197-query corpus set, the 27-case tenant-isolation probe, and the R6-T4 regression floors (`test_r6t4_regression_floors`) | `eval-db` job |
| **Vitest** | `frontend/__tests__/` | Screens, components and hooks: the typed API client's 401 handling (refresh once, then redirect), login, planner turns and policy answers, receipt upload and correction, the app shell, the `/api/version` route | `frontend` job |
| **Playwright e2e** | `frontend/e2e/p7t4/` | The real UI against a live, seeded backend: an employee plans a trip, uploads a receipt and submits a report; a manager approves a report and a trip; finance sees analytics, policy, audit and users; one report handed from employee to manager; forbidden routes show "no access" | `e2e` job |
| **Keyed / live evals** | `evals/manual/` (`@pytest.mark.live`) | Answer quality that needs real models: RAG faithfulness with Gemini and a hosted cross-encoder, live rerank metrics, Gemini gap-fill and narrative quality, a per-stage latency profile | **Never.** Manual, self-skip without keys |
| **Static analysis** | `pyproject.toml`, `frontend/` configs, `scripts/ops/` | Lint and format (ruff, ESLint, Prettier), types (mypy on `backend config evals`, `tsc --noEmit`), one Alembic head, no secrets in the tree or history, configuration files that agree with each other | `backend`, `frontend`, `secret-scan` jobs |

A few tests worth knowing about by name:

- `backend/tests/test_ci_workflow_hygiene.py` parses every workflow file and fails if a
  Playwright run could write an HTML report or trace (which would record the login body and
  tokens), if an artifact upload has no short retention, or if a step could print a secret.
- `backend/tests/test_migration_chain.py` and the `test_migration_00NN_*` files prove the
  migration history and the data migrations on a real database.
- `backend/tests/unit/test_unit_health_commit.py` covers the `commit` field that the deploy
  pipeline waits on (see the [deployment guide](../guides/deployment.md)).

---

## How to run each tier

Full setup is in the [local setup guide](../guides/local-setup.md#running-the-tests). The
commands below are copied from the `Makefile`, `.github/workflows/ci.yml` and the README, and
assume the compose defaults `POSTGRES_USER=atlas_user` / `POSTGRES_PASSWORD=change-me`.

> [!WARNING]
> The DB-backed fixture `_schema` in `backend/tests/conftest.py` runs
> `DROP SCHEMA public CASCADE` on whatever `DATABASE_URL` points at, and an exported variable
> wins over the `conftest.py` default. Always set `DATABASE_URL` inline to a throwaway database
> whose name ends in `_test`, and run one pytest suite at a time against it.

```bash
# Static analysis + hermetic backend suite (mirrors the CI backend job)
ruff check . && ruff format --check . && mypy backend config evals
alembic heads                           # exactly one: 0025_grade_entitlement_rules (head)
DATABASE_URL=postgresql+psycopg2://atlas:atlas@localhost:1/none pytest -m 'not db' --ignore=evals/manual

# Full DB-backed suite (includes the API contract matrix)
docker exec atlas-postgres createdb -U atlas_user atlas_test
DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test \
  PYTHONHASHSEED=0 pytest -m db
git checkout -- ai_agents/rag/abstention_threshold.json ai_agents/rag/rerank_calibration.json

# AI-eval DB tier (mirrors the CI eval-db job; the target pins its own atlas_test_aieval URL)
docker exec atlas-postgres createdb -U atlas_user atlas_test_aieval
make eval-db                            # writes evals/reports/metrics.json

# Frontend checks (mirrors the CI frontend job)
cd frontend && npm ci
npm run lint && npm run format:check && npm run type-check && npm run test && npm run build

# Playwright e2e: see the two-terminal flow in docs/guides/local-setup.md
npm run e2e:p7t4                        # setup + p7t4-employee + p7t4-manager + p7t4-finance
```

Other `Makefile` targets: `make lint`, `make test`, `make ci-local` (lint + test + build),
`make eval` (hermetic and DB eval tiers together).

**Keyed evaluations** (not CI; each self-skips without its key; all except `eval-live` pin
the `atlas_test_aieval` database):

| Target | What it measures | Needs |
|---|---|---|
| `make eval-live-r5` | RAG faithfulness through the full policy agent over the 145 answerable queries (Gemini generation and judge, Novita cross-encoder, local embeddings) | `LLM_API_KEY`, `RERANK_API_KEY` |
| `make eval-rerank-live` / `make eval-rerank-live-novita` | Retrieval metrics with the hosted `bge-reranker-v2-m3` over the same local-embedding candidates | `HF_API_TOKEN` / `RERANK_API_KEY` |
| `make eval-live-matrix` | Embedder upgrade matrix (C0..C3) with OpenRouter embeddings and the Novita reranker | `LLM_API_KEY` (the OpenRouter embeddings adapter reuses it), `RERANK_API_KEY` |
| `make eval-profile-live` | Per-stage latency profile (retrieval, rerank, generation) | `LLM_API_KEY`, `RERANK_API_KEY` |
| `make eval-live` | Gemini gap-fill quality and narrative faithfulness/relevance with a judge calibrated by Cohen's kappa (run `unset DATABASE_URL` first) | `LLM_API_KEY` |

The Novita reranker allows 30 requests a minute; the live RAG tests throttle themselves to
stay under it. Calibration targets (`calibrate-*`, `threshold-*`, `recalibrate`,
`recalibrate-live`) re-fit the Platt parameters and the abstention threshold τ\*; see
[`evals/README.md`](../../evals/README.md).

---

## CI job graph

One workflow, `.github/workflows/ci.yml`, triggered on pushes and pull requests to `main`.
The `needs:` edges below are copied from the file.

```mermaid
flowchart LR
  B["backend<br/>ruff, mypy, pytest,<br/>one alembic head"]
  F["frontend<br/>lint, format, tsc,<br/>vitest, build"]
  V["eval-db<br/>pgvector service,<br/>AI-eval DB tier"]
  S["secret-scan<br/>tree + history,<br/>env audit"]
  E["e2e<br/>Playwright, 3 roles"]
  D["docker-build<br/>both images,<br/>compose smoke"]
  P["deploy-staging<br/>push to main only"]
  B --> E
  V --> E
  B --> D
  F --> D
  B --> P
  F --> P
  V --> P
  E --> P
  D --> P
  S --> P
```

| Job | `needs:` | What it does |
|---|---|---|
| `backend` | none | `ruff check .`, `ruff format --check .`, `mypy backend config evals`, `pytest` (DB tests skip: no database service), then fails unless `alembic heads` reports exactly one head |
| `frontend` | none | Node 20: `npm ci`, `lint`, `format:check`, `type-check`, `test` (Vitest), `build` |
| `eval-db` | none | `pgvector/pgvector:pg16` service, `PYTHONHASHSEED=0`, `LLM_PROVIDER=local`, `EMBEDDINGS_PROVIDER=local`; `pytest backend/tests/eval -m db`; uploads `metrics.json`, `rag_corpus_metrics_table.csv` and `risk_coverage.csv` with 3-day retention |
| `secret-scan` | none | Full-history checkout; `python scripts/ops/secret_scan.py` (tree and every added line in history) and `python scripts/ops/env_audit.py` |
| `e2e` | `backend`, `eval-db` | Postgres and MinIO services, all providers `local`, `RATE_LIMIT_ENABLED=false`; a throwaway seed password generated and masked per run; migrate, seed, start uvicorn, run the `setup` and three `p7t4-*` Playwright projects |
| `docker-build` | `backend`, `frontend` | Builds both images, checks `boto3` imports in the backend image, brings up the full compose stack from `.env.example` and waits for both containers to report healthy |
| `deploy-staging` | all six above | Only on a push to `main`. Calls the Render deploy hook pinned to the tested SHA, waits for that commit on `/health` and `/api/version`, then a 3-role login smoke and a CORS preflight pair. Details: [deployment guide](../guides/deployment.md) |

On a pull request `deploy-staging` is reported as skipped; it runs only after a merge to `main`.

---

## AI accuracy evaluation

The harness in `evals/` treats each AI output as a classification problem and reports
per-metric, per-class numbers against versioned gold datasets. Floors live in
[`evals/thresholds.py`](../../evals/thresholds.py), each set just below the measured value so
that it passes today and fails on a real regression. Background: [`evals/README.md`](../../evals/README.md).

| Metric | Dataset | Result | Gate |
|---|---|---|---|
| **M-A2** receipt ↔ card ↔ trip matching | 53 cases, 65 gold linkage tuples (`m_a2_match.jsonl`) | F1 **1.0** | F1 ≥ 0.95 (hermetic) |
| **M-A3** policy decisions | 66 cases (`m_a3_policy.jsonl`) | Confusion matrix fully diagonal | Violation recall **== 1.0, hard gate** (no silent in-policy pass); flag precision ≥ 0.80 (hermetic) |
| **M-A1** extraction routing and provenance | 85 receipts (`m_a1_extraction.jsonl`) | 85/85 routing paths, 510/510 provenance labels | Routing and provenance accuracy == 1.0; per-field floors 0.80–0.95 (DB tier) |
| **Tenant isolation** (RAG) | 27 queries (`rag_queries.jsonl`), two-org seed | **0** cross-tenant chunks returned | == 0, hard gate (DB tier) |

M-A1 runs with stub OCR and LLM providers owned by the harness, so it validates the pipeline's
plumbing (routing at 0.70 confidence, gap-fill, provenance labels, the database write), not
vision accuracy. Structured outputs are also compared to committed snapshots in
`evals/snapshots/`, so a changed decision shows up as a reviewable diff;
`ATLAS_EVAL_UPDATE=1 make eval` regenerates them deliberately.

---

## RAG metrics

The canonical source for every RAG number is
[`docs/RAG/metrics/FINAL_METRICS_SUMMARY.md`](../RAG/metrics/FINAL_METRICS_SUMMARY.md). Two
families of numbers exist and are never mixed:

- **Local / CI path:** deterministic local embedder and reranker, cosine gate. Reproducible
  byte for byte. These prove the wiring works and does not regress; they are not a claim
  about answer quality.
- **Keyed path:** Gemini via OpenRouter and a hosted `bge-reranker-v2-m3` cross-encoder, with
  the calibrated τ\* gate. Network-bound and run by hand.

Gold set: **197 queries** (145 answerable, 52 unanswerable) over a four-document corpus. The
figures below were measured on the 208-unit corpus; it has 219 units today, and the 11 units
added in phase 10 moved no gold unit into or out of any top-10 (the keyed runs were not
repeated on it).

| Metric | Dense baseline (R0) | Hybrid + RRF (local, CI-gated) | + Cross-encoder (keyed) | Source |
|---|:-:|:-:|:-:|---|
| recall@10 | 0.727 | 0.819 | **0.959** | `metrics.json`; `rag_corpus_hf_rerank_live.json` |
| nDCG@5 | — | 0.658 | **0.873** | same |
| MRR | 0.679 | 0.760 | **0.924** | same |
| AURC (lower is better) | 0.101 | 0.084 | **0.047** | `metrics.json`; `ai_agents/rag/abstention_threshold.json` |
| Faithfulness @ coverage | — | — | **0.981 @ 74% grounded** (108/145) | keyed `make eval-live-r5`, C7R run 2026-11-11 |

CI floors on the local path (from `evals/thresholds.py`, checked by
`test_r6t4_regression_floors`): recall@10 ≥ 0.80, nDCG@5 ≥ 0.63, precision@5 on answered
≥ 0.20, AURC ≤ 0.09, coverage == 1.0, abstention == 0.019231, plus per-document-type recall
floors. The faithfulness floor `RAG_FAITHFULNESS_FLOOR = 0.95` and the grounded-coverage floor
`RAG_GROUNDED_COVERAGE_FLOOR = 0.70` (at least 102 of 145 answered) apply only to the keyed run;
CI has no LLM judge and never enforces them. CI does check the coverage floor's arithmetic
(`backend/tests/eval/test_rag_live_gates.py`).

Faithfulness is always quoted with its coverage: a system that answers 5% of questions can
score 1.0 and be useless. Latest keyed run: 411 of 415 claims supported; 108 grounded, 36
`needs_review`, 1 abstained; the two flagged answers cite FAQ clauses that state the claim but
fall outside the judge's top-5 context, so there are no confirmed model hallucinations. The
first measurement, at R6, was 0.991 @ 78% grounded (113/145); the later per-sentence support
gate traded some coverage for sentence-level support. Fused retrieval latency on the local path
is p50 about 4–7 ms across recorded runs (machine-dependent). Keyed per-stage latency was not
captured (see the "not measured" section of the metrics summary).

`metrics.json` and the live-rerank JSON named in the source column are regenerated reports
(`evals/reports/` is gitignored); the metrics summary cites the committed phase log behind each
cell.

---

## Determinism strategy

Every CI tier runs with no API keys and gives the same answer on every run. How:

| Mechanism | Where |
|---|---|
| Every provider (LLM, OCR, embeddings, rerank, storage, email) has a `local` or `console` implementation and defaults to it | `services/*/factory.py`, `config/settings/base.py` |
| The root `conftest.py` pins `LLM_PROVIDER=local`, `EMBEDDINGS_PROVIDER=local`, `RATE_LIMIT_ENABLED=false` and `DEMO_RESET_SCHEDULE=off` with `setdefault`, so a stray `.env` cannot switch a test to a real provider | `conftest.py` |
| The local embedder is feature hashing over keyed `blake2b` digests, stable across processes; `PYTHONHASHSEED=0` is still pinned in the `Makefile`, CI and compose as a second guard | `services/embeddings_provider/local.py` |
| The local reranker is anchored to the RRF order, so the local path keeps recall@10 at the hybrid value | [`ADR-003`](../RAG/adr/ADR-003-cross-encoder-reranking.md) |
| Each DB test runs inside an outer transaction that is always rolled back, even when the code under test commits | `db_session` in `backend/tests/conftest.py` |
| e2e uses `STORAGE_PROVIDER=local`, self-seeded fixtures and a planner driven by chips, so the suite can be re-run against the same database | `frontend/e2e/p7t4/`, [P7-T4 log](../phase_logs/P7-T4-e2e-tests.md) |
| AI-eval outputs are snapshot-compared, not only threshold-checked | `evals/snapshots/` |
| Keyed evaluations are marked `live` and skip themselves without a key, so they can never turn CI red or green | `evals/manual/` |

The cost of this design is that CI cannot see problems that only appear with real models. The
next section is the clearest example.

---

## The R6 story: a green CI that hid a broken live path

During the last RAG phase the first keyed run returned **0 of 145** answerable questions as
grounded, while every CI job was green.

- **Cause.** The citation contract required a confidence of at least 0.70. That floor was set
  when confidence was a cosine similarity. An earlier phase switched confidence to a
  Platt-calibrated reranker probability whose operating threshold is τ\* ≈ 0.039 (maximum
  observed about 0.25), so on the keyed path the floor could never be met. CI runs the cosine
  path, where 0.70 is reachable, so CI could not catch it.
- **Fix.** `resolve_citation_threshold` makes the floor scale-aware: τ\* on the calibrated
  path, 0.70 on the cosine path (so the CI path stayed byte-identical). Live grounding went
  from 0 to 113/145. (A later UAT fix, PR #10, gave the local-reranker cosine path its own
  0.55 floor plus a question-relevance check; any other fallback keeps 0.70.)
- **A second defect, in the measurement.** Faithfulness first measured 0.775. The holistic
  judge was matching citation labels as text inside chunk text, where labels never appear. A
  statement-level judge that excludes citation-attribution statements, with the same
  binary-per-answer aggregation, measured 0.991 in a fresh keyed run (113 grounded answers,
  against 111 before). The code change was to the judge only.

The full account, with the decisions and gate evidence, is in
[`R6_gate_and_judge_fix_asbuilt.md`](../phase_logs/R6_gate_and_judge_fix_asbuilt.md). The
lesson I took from it: a deterministic CI proves the wiring; it does not prove the product
works with real models, so the keyed evaluation has to be run and read before calling a RAG
change done.

---

## Known test caveats

- **One suite at a time per database.** The session `_schema` fixture drops and recreates the
  schema, so two pytest processes on one database interfere (seen once in the phase 11 gate
  and resolved by re-running alone).
- **The DB suite rewrites two calibration files.** The fit tests rewrite their local sections
  of `ai_agents/rag/abstention_threshold.json` and `rerank_calibration.json`; restore them
  with `git checkout --` after a run.
- **e2e and the Docker build are CI's job.** The local merge gates run the pytest, eval-db
  and Vitest tiers; Playwright and the image build are proven by the CI run on the PR (the
  phase 11 gate log says so explicitly).
- **M-A1 uses stub OCR.** Vision OCR quality on real receipts is exercised manually on the
  live demo, not measured by a gated metric.
