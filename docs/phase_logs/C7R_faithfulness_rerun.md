# C7R — Keyed faithfulness re-run (as-built)

**Date:** 2026-11-11
**Author:** Garv Singh Saini
**Commit:** `7bfd25b` (branch `c7r-fixes-backup`), squash-merged to `main` in PR #10 (`2fbe114`, 2026-11-14)

## Summary
- Keyed `make eval-live-r5` re-run after the C7R per-sentence support gate: **0.981 faithful @ 74% grounded (108/145)**.
- Claim support 0.990 (411/415). The two flagged answers are judge-context misses, not confirmed hallucinations.
- This replaces the R6 figure (0.991 @ 78%, 113/145) as the canonical number; the gate traded five grounded answers for sentence-level support.

## Setup
| Item | Value |
|---|---|
| Harness | `evals/manual/test_rag_corpus_ragas_live.py` (`@pytest.mark.live`, self-skips without keys) |
| Command | `make eval-live-r5` |
| Generation and judge | One provider instance (`get_llm_provider()`), `LLM_PROVIDER=openrouter`. The commit records the run as "Gemini + Novita"; the exact model id is not logged. The model comes from `LLM_MODEL` (the `Makefile` target does not set it): the local environment sets `google/gemini-2.5-flash`, and the committed `LLM_MODEL_FALLBACK` default is `google/gemini-2.5-flash-lite`. |
| Judge method | Statement-level decomposed judge, attribution-excluded (`_judge_faithful_decomposed`, introduced at R6); an answer counts as faithful only if every substantive claim is supported |
| Reranker | Novita `baai/bge-reranker-v2-m3`, throttled to ≤30 batches/min (two batches per query) |
| Embeddings | Local (`EMBEDDINGS_PROVIDER=local`, `PYTHONHASHSEED=0`); HyDE off (committed default) |
| Gold set | 197 queries (145 answerable, 52 unanswerable) over the 208-unit corpus; the run uses all 145 answerable |
| Judge context | Parent contexts of the top-5 chunks from a separate retrieval call in the harness (`_K = 5`) |

## Results
| Metric | C7R (2026-11-11) |
|---|---|
| Faithfulness (binary per grounded answer) | **0.981** |
| Grounded coverage | **108/145 (74%)** |
| Claim support | 0.990 (411/415 claims) |
| Flagged answers | 2 |
| Confirmed model hallucinations | 0 |
| `RAG_FAITHFULNESS_FLOOR` (keyed-only) | 0.95 — passed |

Gates recorded on the same commit: hermetic 1690 passed; full DB suite 2734 passed, 0 failed;
vitest 443 passed; eval-db unchanged (recall@10 0.819031, coverage 1.0, abstention 0.019231);
alembic single head `0014`.

## Disposition breakdown (145 answerable)
| Disposition | Count |
|---|---|
| grounded (judged) | 108 |
| `needs_review` (provisional or withheld) | 36 |
| abstained (the harness prints this as `silent`) | 1 |

## The two flagged answers
Both cite FAQ clauses, FAQ-Q21 and FAQ-Q12, that state the claim. Those clauses were not in the
judge's context: the judge sees the top-5 parent contexts from the harness's own retrieval call,
while the agent answers from its own retrieval inside `GroundedRetriever`, so a clause the agent
cited can fall outside what the judge is shown. The four unsupported claims (415 − 411) are in
these two answers. Because the cited text supports them, they are not counted as model
hallucinations. The per-query IDs of the two answers were not committed.

## Comparison with R6 and why coverage moved
| | R6 (2026-07-05) | C7R (2026-11-11) |
|---|---|---|
| Faithfulness @ coverage | 0.991 @ 78% (113/145) | 0.981 @ 74% (108/145) |
| Claim support | 0.997 (387/388) | 0.990 (411/415) |
| Disposition (grounded / needs_review / abstained) | 113 / 31 / 1 | 108 / 36 / 1 |
| Judge | statement-level, attribution-excluded | same |

The judge did not change between the runs; the answer path did.
- **Per-sentence support gate** (`ai_agents/rag/support.py`, commit `deecfb1`, 2026-11-09). Each
  cited sentence is checked against the clause it cites: `low_support` (below
  `NLI_ENTAILMENT_THRESHOLD` 0.55), `number`, `direction`, `unanchored`, `polarity`, `contrast`
  (mis-citation) and `uncited`. An answer is grounded only if every sentence passes; otherwise
  it is returned as `needs_review`, showing the passing sentences provisionally or withholding
  the draft (for example when a dropped sentence states a condition).
- **Grounding recovery** (`7bfd25b`). The gate had caused a grounding regression on the
  calibrated path; this commit fixed grouped and trailing citation markers, over-eager
  `contrast` and `polarity` checks and section-sibling citations, and hardened against bare
  verdicts, question echoes and shifted trailing markers. The local-path floor relaxation in the
  same commit (0.55) does not apply here: this run uses the calibrated Novita path.

The harness itself changed in only one way between the runs (`deecfb1`): its own
`check_entailment` probe, which feeds the reported `nli_entail_rate`, now receives `question=`
and the top-K `retrieved=` set, the same inputs as the production gate. The disposition counts
come from the agent's own result, not from this probe.

Net effect: grounded fell by five (113 → 108) and `needs_review` rose by five (31 → 36). No
per-query diff between the two runs was kept, so individual answers that changed disposition are
not known, and the shift is attributed to the gate as a whole, not to a specific check.

## Reproducing
1. Have the local Postgres + pgvector database `atlas_test_aieval` that `make eval-db` uses
   (the DSN is in the `Makefile`).
2. Export `LLM_API_KEY` (OpenRouter), `LLM_MODEL=google/gemini-2.5-flash` and `RERANK_API_KEY` (Novita).
3. Run `make eval-live-r5`. The summary block prints faithfulness, disposition, claim fraction
   and each unsupported claim with the judge's reason. Expect a long run: rerank batches are
   spaced at least 2.1 s apart, two per query, and the R6 run took 1006 s of wall time.

## Limitations
- No machine-readable artifact is committed: `evals/reports/*` is gitignored. The committed
  record is this log, the README results note and `FINAL_METRICS_SUMMARY.md`.
- A keyed run spends API credit on OpenRouter and Novita; CI never runs it or gates on it.
- The judge is an LLM and generation is non-deterministic, so a re-run can differ by a few
  answers. The 0.95 floor leaves about 3 points of headroom for that.
- The judge and the generator are the same model.

## Links
- [`R6_gate_and_judge_fix_asbuilt.md`](R6_gate_and_judge_fix_asbuilt.md): the first measurement and the judge fix
- [`phase_8_completion_c7r_fixes.md`](phase_8_completion_c7r_fixes.md): the support gate's checks
- [`FINAL_METRICS_SUMMARY.md`](../RAG/metrics/FINAL_METRICS_SUMMARY.md) §4: canonical rows
- [`RAG_ARCHITECTURE.md`](../RAG/RAG_ARCHITECTURE.md#measurement--honest-evaluation)
- `evals/thresholds.py` (`RAG_FAITHFULNESS_FLOOR`), `evals/manual/test_rag_corpus_ragas_live.py`
