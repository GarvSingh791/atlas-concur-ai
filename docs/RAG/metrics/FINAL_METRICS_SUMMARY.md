# ARE — Final Metrics Summary (Canonical)

**This file is the single source of truth for every number quoted about the Atlas
Retrieval Engine (ARE)** — in `docs/RAG/`, in `evals/`, and in the top-level README.
It is for anyone checking a figure: a reviewer who wants the headline sheet (next
section), or an engineer who wants each value's dataset, run condition and source.
If a number does not appear here, it was not measured — see
[Untraceable / not-yet-measured](#untraceable--not-yet-measured).

> **Read this first — two number families, never conflated.**
> Atlas runs the same pipeline two ways:
> - **Local / CI path** (`LLM_PROVIDER=local`, deterministic blake2b embedder,
>   RRF-anchored local reranker, cosine gate). Byte-reproducible, no network, no
>   key. This is what CI gates. Its numbers are **plumbing** numbers — they prove
>   the wiring is correct and non-regressing, *not* production answer quality.
> - **Keyed path** (`LLM_PROVIDER=openrouter` → Gemini, a hosted
>   `bge-reranker-v2-m3` cross-encoder, calibrated τ\* gate). Network-bound, not
>   byte-reproducible, run by hand (`make eval-live-r5`, `make eval-rerank-live*`).
>   These are the **calibrated quality** numbers.
>
> Faithfulness and the cross-encoder quality lift exist **only** on the keyed
> path. CI structurally cannot measure them (it has no LLM judge and no real
> reranker). A local number is never presented as a quality claim. The public
> staging deployment runs the local embedder and reranker with Gemini (via
> OpenRouter) for generation, so it does not reproduce the keyed column.

---

## Headline sheet

| Metric | Value | Dataset | Condition | Source |
|---|---|---|---|---|
| recall@10 — dense-only baseline (R0) | 0.727 | 145 answerable gold queries | local path | `R0-T3_T6_asbuilt.md:49` |
| recall@10 — hybrid + RRF | **0.819** (0.819031) | 145 answerable | local path, **CI-gated** (floor 0.80) | `R1-T3_T5_asbuilt.md:56`; `evals/thresholds.py:135` |
| recall@10 — + live cross-encoder | **0.959** (0.958916) | 145 answerable | keyed: hosted `bge-reranker-v2-m3` over local-embedding candidates | `R2-T1_T2_asbuilt.md:214–215` |
| nDCG@5 — hybrid → + cross-encoder | 0.658 → **0.873** | 145 answerable | local → keyed | `R2-T1_T2_asbuilt.md:211` |
| AURC (lower is better) — R0 → reranker signal | 0.101 → **0.047** | R0: 197-query gold set; reranker: calibration set, n = 155 | R0 local path → keyed reranker signal (different sets; like-for-like on the same 155: cosine 0.088 → reranker 0.047) | `R0-T3_T6_asbuilt.md:57`; `ai_agents/rag/abstention_threshold.json` |
| τ\* operating point | **0.039299** → coverage 0.742, risk 0.043 (≈95.7% precision on answered) | n = 155 | keyed, target risk 0.05 | `abstention_threshold.json`; `R3-T1_T2_asbuilt.md:78` |
| Faithfulness @ coverage (current) | **0.981 @ 74% grounded** (108/145); 411/415 claims supported; 0 confirmed hallucinations | 145 answerable | keyed C7R run, 2026-11-11 (Gemini via OpenRouter + Novita reranker, local embeddings) | `README.md` § Quality & Evaluation (results note written with the run; merged in PR #10) |
| Faithfulness @ coverage (history) | 0.991 @ 78% grounded (113/145); 387/388 claims | 145 answerable | keyed R6 run, before the per-sentence support gate | `R6_gate_and_judge_fix_asbuilt.md:12,71` |
| CI eval tier | `eval-db`: 48 tests; recall@10 0.819, coverage 1.0, abstention 0.019231 | 197 gold queries | local path, every push | `phase_10_known_limits_grade.md:106–108` |
| Fused retrieval latency | p50 ≈ 4–7 ms across recorded local runs | 197 queries | local path, machine-dependent | §5 |

**Corpus note.** All figures above were measured on the **208-unit** corpus. The corpus
now has **219 units** (policy 100 · faq 59 · howto 35 · reference 25): phase 10 added 11
traveller-grade units (DEF-09/DEF-10, FAQ-Q51–Q59). A per-query diff on the same harness
showed **no gold unit entering or leaving any top-10**, and eval-db stayed at recall@10
0.819, coverage 1.0, abstention 0.019231 (`docs/phase_logs/phase_10_known_limits_grade.md:99–108`).
The keyed runs were not repeated on the 219-unit corpus.

---

## 1. Evaluation methodology

| Property | Value | Source |
|---|---|---|
| Corpus at measurement | 4 documents, **208 units/chunks per org** (policy 98 · faq 50 · howto 35 · reference 25), one chunk per unit | `docs/phase_logs/R0-T2_corpus.md:16`; `R0-T3_T6_asbuilt.md:20` |
| Corpus today | **219 units** (policy 100 · faq 59 · howto 35 · reference 25) — see the corpus note above | `database/seeds/corpus/`; `phase_10_known_limits_grade.md:99–101` |
| Gold query set | **197 queries = 145 answerable + 52 unanswerable** | `evals/datasets/rag_corpus_queries.jsonl` |
| Answerable query types | rule 55 · howto 33 · conversational 25 · cross_document 15 · threshold 13 · trip_planning 4 | `rag_corpus_queries.jsonl` (`type` field) |
| Retrieval `k` | k = 10 (also reported at k = 5); production `RETRIEVAL_TOP_K = 5` | `config/settings/base.py:141` |
| Embedding dim | 1536 | `config/constants/embeddings.py:8` |
| CI gate | deterministic local path; **faithfulness is never CI-gated** (no LLM judge in CI) | `evals/reports/README.md`; `evals/README.md` |
| Keyed runs | `make eval-live-r5` (faithfulness), `make eval-rerank-live` / `eval-rerank-live-novita` (retrieval), `make eval-profile-live` (latency) — all self-skip without keys | `Makefile`; `evals/manual/` |

The gold set is hand-authored over the corpus: every answerable item lists the
`(doc_id, unit_id)` pairs that answer it; `cross_document` items genuinely need
≥2 documents; unanswerable items are outside the whole corpus and **must abstain**.

**About the sources.** `evals/reports/*` (`metrics.json`, the CSVs, the live-rerank
JSONs) is **gitignored**: it is regenerated on every `make eval-db` run and uploaded by
CI as the `ai-eval-metrics` artifact, so it is not in the public tree. Each headline
figure therefore also cites the committed phase log (`docs/phase_logs/`) or committed
artifact (`ai_agents/rag/*.json`, `evals/thresholds.py`) that recorded it. A few
secondary cells (keyed nDCG@10 / MAP / MRR / hit@10 / precision@10, per-query-type
nDCG, local latency) exist only in those regenerated reports; they were read from the
report at documentation time and are reproducible with the make target named in each
footnote.

**Re-run on the 219-unit corpus.** A local `make eval-db` on the current tree
(2026-11-15, report not committed) reproduced every set metric exactly — recall@10
0.819031, precision@5 0.244138, coverage 1.0, abstention 0.019231, per-doc-type recall —
while the rank-sensitive values moved slightly: nDCG@5 0.655706 (was 0.658420), nDCG@10
0.687489 (0.690157), MRR 0.754097 (0.760496), AURC 0.0837 (0.084096). All remain inside
the CI floors in §7. The tables below keep the 208-unit values.

---

## 2. Retrieval quality — the three honest tiers

The system was measured at three fidelity levels. We report all three because
collapsing them would be the sleight-of-hand this project exists to avoid.

| Metric (k=10 unless noted) | R0 dense-only baseline | Hybrid (dense+sparse+RRF), **local/CI** | + Cross-encoder rerank, **keyed** | Source |
|---|---|---|---|---|
| recall@10 | **0.726667** | **0.819031** | **0.958916** | ¹ ² ³ |
| nDCG@10 | 0.596470 | 0.690157 | 0.889097 | ¹ ² ³ |
| MAP | 0.506033 | 0.603025 | 0.834542 | ¹ ² ³ |
| MRR | 0.679274 | 0.760496 | 0.924080 | ¹ ² ³ |
| hit@10 | 0.917241 | 0.979310 | 1.000000 | ¹ ² ³ |
| precision@10 | 0.121379 | 0.140000 | 0.171034 | ¹ ² ³ |
| nDCG@5 | — | 0.658420 | 0.873061 | ² ³ |
| precision@5 (on answered) | — | 0.244138 | 0.324138 | ² ³ |

¹ Dense-only R0 baseline: committed record `R0-T3_T6_asbuilt.md:49`; full precision in
  `metrics.json rag_corpus.baseline_r0.overall` and `evals/thresholds.py:132`.
² Hybrid RRF, local path: committed record `R2-T1_T2_asbuilt.md:63–68` (recall/nDCG@10/
  MAP/MRR) and `R6-T3_T4_asbuilt.md:125–144` (nDCG@5, precision@5, AURC); regenerated by
  `make eval-db` into `metrics.json rag_corpus.overall` / `overall_at_5` and
  `rag_corpus_metrics_table.csv`.
³ Keyed: a live run of hosted `baai/bge-reranker-v2-m3` (HuggingFace Inference) over the
  same local-embedding candidates, `make eval-rerank-live` (2026-06-28). Committed record of
  recall@5/@10, nDCG@5 and precision@5: `R2-T1_T2_asbuilt.md:211–214`; the remaining cells
  are from that run's report, `evals/reports/rag_corpus_hf_rerank_live.json` (regenerated,
  not committed). 17 of 197 queries fell back to RRF order on cold-start timeouts, so the
  keyed column is a lower bound (`R2-T1_T2_asbuilt.md:211–213`).

**How to read this.** Hybrid retrieval lifts recall@10 by **+0.092** over the
dense-only baseline (`0.727 → 0.819`) on the deterministic local path — this is the
CI-gated plumbing number. Adding the real cross-encoder on the keyed path lifts
recall@10 to **0.959** and nDCG@5 from `0.658 → 0.873` (**+0.215**). The local
reranker is RRF-anchored by design (it preserves RRF order unless a candidate wins
decisively), so on the local path recall stays at the RRF value `0.819` — the
cross-encoder's quality contribution is real but lives on the keyed path.
See [ADR-003](../adr/ADR-003-cross-encoder-reranking.md).

### Per-query-type (hybrid, local path, k=10)

Read from `metrics.json rag_corpus.by_type` (regenerated by `make eval-db`) on the
208-unit corpus. recall@10 is unchanged on the 219-unit corpus; nDCG@10 moves in the third
decimal place.

| Type | n | recall@10 | nDCG@10 | Source |
|---|---|---|---|---|
| conversational | 25 | 0.726667 | 0.711612 | `metrics.json rag_corpus.by_type.conversational` |
| howto | 33 | 1.000000 | 0.836638 | `…by_type.howto` |
| rule | 55 | 0.833333 | 0.703046 | `…by_type.rule` |
| threshold | 13 | 0.923077 | 0.575278 | `…by_type.threshold` |
| cross_document | 15 | 0.588889 | 0.508883 | `…by_type.cross_document` |
| trip_planning | 4 | 0.231548 | 0.223506 | `…by_type.trip_planning` |

`trip_planning` (n=4) is the hardest slice and the direct descendant of the
originating "trip to New York" failure (see
[ENGINEERING_DECISIONS](../ENGINEERING_DECISIONS.md)); its small n makes the cell a
low-support signal, not a headline.

### Per-document-type (hybrid, local path) — with `n_answered`

Committed record: `R6-T3_T4_asbuilt.md:143–149`; regenerated as `rag_corpus_metrics_table.csv`.

| doc_type | n_answered | recall@10 | nDCG@5 | precision@5 (answered) | Source |
|---|---|---|---|---|---|
| overall | 145 | 0.819031 | 0.658420 | 0.244138 | `rag_corpus_metrics_table.csv` |
| cross_document | 92 | 0.736517 | 0.613407 | 0.278261 | " |
| howto | 33 | 1.000000 | 0.836638 | 0.200000 | " |
| policy | 15 | 0.866667 | 0.569433 | 0.160000 | " |
| reference | 4 | 1.000000 | 0.471713 | 0.150000 | " |
| faq | 1 | 1.000000 | 1.000000 | 0.200000 | " |

> `faq` (n=1) and `reference` (n=4) are **low-support cells** — a recall of `1.000`
> at n=1 is an artifact of the tiny answerable set for that doc-type, not evidence
> of strength. `n_answered` is printed precisely so these read as artifacts.
> `precision@5-on-answered` is structurally bounded here (~1–2 relevant clauses per
> query among the top-5), so it is a collapse-guard, not a quality target.

---

## 3. Calibration & abstention (keyed reranker signal)

Abstention is decided by a **Platt-calibrated** reranker score against a
**risk-coverage-selected** threshold τ\*. Not conformal prediction — see
[ADR-004](../adr/ADR-004-confidence-calibration.md).

| Quantity | Value | Source |
|---|---|---|
| τ\* (operating threshold) | **0.039299** | `ai_agents/rag/abstention_threshold.json` (`novita`/`huggingface`); `R3-T1_T2_asbuilt.md:78` |
| AURC — reranker signal | **0.046982** | `abstention_threshold.json` (`aurc_reranker`) |
| AURC — cosine signal | 0.087511 | `abstention_threshold.json` (`aurc_cosine`) |
| AURC — R0 dense baseline | 0.101279 | `R1-T3_T5_asbuilt.md:63`; `R0-T3_T6_asbuilt.md:57` |
| AURC — hybrid, local cosine signal | 0.084096 | `R1-T3_T5_asbuilt.md:63`; `R6-T3_T4_asbuilt.md:127` |
| coverage @ τ\* | 0.741935 | `abstention_threshold.json` (`coverage_at_tau`) |
| risk @ τ\* | 0.043478 (⇒ ≈95.65% precision-on-answered) | `abstention_threshold.json` (`risk_at_tau`); `R3-T1_T2_asbuilt.md:78` |
| target risk r\* | 0.05 | `abstention_threshold.json` (`target_risk`) |
| calibration set n | 155 | `abstention_threshold.json` (`n_queries`) |
| Platt params (novita/hf) | a = −2.469343, b = 3.580282 | `ai_agents/rag/rerank_calibration.json` |

**AURC headline (lower is better): `0.101 → 0.047`** — the R0 dense-only baseline
(`0.101279`) versus the keyed reranker signal (`0.046982`). On the *same* keyed
queries the reranker signal roughly **halves** AURC versus the cosine signal
(`0.087511 → 0.046982`, ≈46% lower) — i.e. the cross-encoder score is a materially
better abstention discriminator than raw cosine. τ\* is provider-keyed: the
`novita`/`huggingface` cross-encoders carry `τ* = 0.039299`; the deterministic
`local` provider is `not promoted` (`tau = null`) and falls back to the cosine gate.
The calibration was fitted on the HuggingFace-hosted run; the `novita` entries carry the
same τ\* and Platt parameters forward because Novita serves the identical
`bge-reranker-v2-m3` model — they were **not re-fitted on Novita** (provenance in each
entry's `note`; `R4-live-matrix_asbuilt.md:60–64`).

---

## 4. Faithfulness — always paired with coverage (keyed only)

Faithfulness is measured **only** on the keyed run and is reported **only** as a
value at an answer-rate. It was **first measured at R6**; there is no per-phase
faithfulness curve and no hallucination-rate time series. Two keyed runs exist.

**Current figure (latest keyed run, C7R, 2026-11-11).** Run after the per-sentence
support gate (`ai_agents/rag/support.py`) was added, with Gemini via OpenRouter and the
Novita cross-encoder, on local embeddings, over the 145 answerable gold queries of the
208-unit corpus (`make eval-live-r5`).

| Quantity | Value | Source |
|---|---|---|
| **Faithfulness @ coverage** | **0.981 faithful @ 74% grounded (108/145 answered)** | `README.md` § Quality & Evaluation (results note written with the run; merged in PR #10) |
| Claim fraction | 0.990 (411/415 claims supported) | same |
| Disposition | grounded 108 · needs_review 36 · abstained 1 | same |
| Confirmed model hallucinations | **0** (the two flagged answers cite FAQ clauses FAQ-Q21 / FAQ-Q12 that state the claim but sit outside the judge's top-5 context) | same |
| Faithfulness floor | `RAG_FAITHFULNESS_FLOOR = 0.95` (keyed-only; CI never enforces it) | `evals/thresholds.py:176` |

The per-run JSON report of this run is not committed (`evals/reports/*` outputs are
regenerated, not versioned); the figures above are the ones recorded in the README results
note written alongside the run, which is the committed public record of it.

**History: first measurement (R6).** Before the per-sentence support gate.

| Quantity | Value | Source |
|---|---|---|
| Faithfulness @ coverage | 0.991 faithful @ 78% grounded (113/145 answered) | `docs/phase_logs/R6_gate_and_judge_fix_asbuilt.md:12–13,71,100–102` |
| Grounded answers judged | 113/113 | `R6_gate_and_judge_fix_asbuilt.md:71` |
| Claim fraction | 0.997 (387/388 substantive claims supported) | `R6_gate_and_judge_fix_asbuilt.md:70–72` |
| Disposition | grounded 113 · needs_review 31 · silent 1 | `R6_gate_and_judge_fix_asbuilt.md:12,71` |
| Genuine model hallucinations | 0 (the lone flag, `ans-052`, is a verified judge error) | `R6_gate_and_judge_fix_asbuilt.md:73–75` |

The correct phrasing everywhere in this package is **"0.981 faithful @ 74%
grounded"** (current), with 0.991 @ 78% quoted only as the R6 history. A naked
faithfulness number is meaningless — a system that answers 5% of queries can score
1.0 and be useless. The stricter gate traded some coverage (78% → 74%) for
sentence-level support. Faithfulness *rose* from a measured `0.775` to `0.991` at R6,
but that delta was a **measurement fix, not a generation fix**
(§6, [ADR / architecture](../RAG_ARCHITECTURE.md#measurement--honest-evaluation)).

---

## 5. Latency (measured) — and what was not measured

Retrieval is measured and fast; the reranker network hop and generation dominate
wall-clock but their per-stage keyed medians were **deferred, not captured**. Full
lens: [PERFORMANCE_PROFILE](../PERFORMANCE_PROFILE.md).

| Stage | Measured figure | Label | Source |
|---|---|---|---|
| Dense retrieval (local, n=197) | p50 2.049 ms · p95 2.982 ms | p50/p95 | `metrics.json rag_corpus.latency_ms` (one local run at documentation time; regenerated, machine-dependent) |
| Fused retrieval, dense+sparse+RRF (local, n=197) | p50 3.9 ms · p95 5.948 ms | p50/p95 | same run as above |
| Fused retrieval (R1 as-built, 208 rows) | p50 ~7 ms · p95 ~10 ms | p50/p95 | `R1-T3_T5_asbuilt.md:70–71` |
| Fused retrieval (R4 contextual-ON run) | p50 4.8 ms · p95 8.3 ms | p50/p95 | `R4-T1_T2_asbuilt.md:124` |
| Dense read `EXPLAIN` @ 208 rows | 0.686 ms (ef_search=100) / 0.844 ms (unset) — **exact-scan; HNSW inert / forward-looking** | wall | `R6-T1_T2_asbuilt.md:11` |
| Reranker — HuggingFace **free tier** | p50 ~5.9 s · p95 ~90 s (cold-start inflated) | p50/p95 | `R2-T1_T2_asbuilt.md:235–241`; `rag_corpus_hf_rerank_live.json` |
| Reranker — Novita (keyed configuration) | "sub-second" (a single live smoke call) | — | `R4-live-matrix_asbuilt.md:56` |
| Reranker — Cohere (managed) | ~595 ms is a **reference figure quoted in planning, not an Atlas measurement**: the Cohere adapter was never run live (no key; live smoke skipped) | — | `R2-T1_T2_asbuilt.md:129,241` |

> The live per-stage profiler (`evals/manual/test_rag_latency_profile_live.py`)
> emits **min/median/max** (not p50/p95) and self-skips without a key; its keyed
> run was **DEFERRED** (`R6-T1_T2_asbuilt.md:89`). So a Novita per-stage median and
> an end-to-end generation median are **not** in any committed artifact.

---

## 6. The R6 measurement arc (why faithfulness moved without touching generation)

Two defects were found and fixed at R6. Both are documented, not hidden — they are
the strongest engineering signal in this package. Canonical technical account:
[RAG_ARCHITECTURE §Measurement & Honest Evaluation](../RAG_ARCHITECTURE.md#measurement--honest-evaluation).

- **Confidence-scale bug.** A `0.70`-scale citation-contract floor was applied to a
  Platt-calibrated signal whose operating point is `τ* ≈ 0.039` (max observed
  ~0.25), making grounding structurally unsatisfiable on the calibrated path → live
  grounded **0/145**, while CI stayed green because CI runs the cosine path where
  0.70 is reachable. This is the local-vs-live gap a deterministic CI cannot catch.
  Fixed with scale-aware `resolve_citation_threshold` → live grounding **0 → 113/145**.
  Source: `R6_gate_and_judge_fix_asbuilt.md:17–42`.
- **Faithfulness measurement defect.** The original holistic judge string-matched
  citation-attribution labels against label-free chunk text, understating
  faithfulness at **0.775**. A statement-level, attribution-excluded judge (same
  binary-per-answer aggregation) measured **0.991** on the identical answers — a
  judge-quality delta only. Source: `R6_gate_and_judge_fix_asbuilt.md:53–79`.

---

## 7. CI regression floors (committed; gate the local path)

These lock the deterministic local-path plumbing and **fail CI on a drop**. They are
*not* the production calibrated numbers. Source: `evals/thresholds.py`.

| Floor / ceiling | Value | Measured (local) |
|---|---|---|
| recall@10 ≥ | 0.80 | 0.819031 |
| nDCG@5 ≥ | 0.63 | 0.658420 |
| precision@5-on-answered ≥ | 0.20 | 0.244138 |
| AURC ≤ | 0.09 | 0.084096 |
| coverage == | 1.0 | 1.000000 |
| abstention == | 0.019231 | 0.019231 |
| per-doc-type recall (faq/howto/policy/reference) ≥ | 0.95 / 0.95 / 0.55 / 0.65 | — |
| faithfulness ≥ (keyed-only) | 0.95 | 0.981 (latest keyed run; R6: 0.991) |

---

## Untraceable / not-yet-measured

Recorded here for honesty; **not** written as facts anywhere in the package.

- **Per-stage keyed latency** (a Novita rerank median, an end-to-end generation
  median): not captured — the live profiler run was deferred (§5). Figures of
  "retrieval ~12 ms / rerank ~911 ms / generation ~4.6 s" that circulated in
  planning **do not appear in any repository artifact** and are therefore excluded.
- **Per-phase faithfulness / hallucination-rate history**: does not exist —
  faithfulness was first measured at R6 (§4).
- **Keyed runs on the 219-unit corpus**: not repeated; every keyed figure here is from
  the 208-unit corpus.

**Measured locally but not committed.** A `make eval-rerank-live-novita` run
(2026-11-11; its report `evals/reports/rag_corpus_novita_rerank_live.json` is gitignored)
recorded, over 197 queries with warm-up excluded, a Novita rerank call latency of
p50 1353 ms / p95 1643 ms per batch (up to 50 candidates), with 0 fail-safe fallbacks, and
recall@10 0.968 / nDCG@5 0.889. Because that report is not in the public tree these
values are **not** used as headlines; they are recorded here so the single "sub-second"
smoke in §5 is not read as a steady-state median.

**Recorded in prose only.** The C7R local-cosine-floor check quoted in
[RAG_ARCHITECTURE §Measurement (c)](../RAG_ARCHITECTURE.md#measurement--honest-evaluation)
(an agent-level run on real Gemini drafts: 0/54 unanswerable or off-topic questions
grounded; answerable grounding 5 → 12 of 27) was written into the architecture doc with
the C7R change (PR #10); no machine-readable report of that run is committed. It is a
diagnostic for the local path, not a headline metric.

---
*All figures read directly from the repository at documentation time. Where this
file and any planning prose disagree, the repository artifact wins.*
