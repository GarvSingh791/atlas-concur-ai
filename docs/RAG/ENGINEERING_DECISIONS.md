# Engineering Decisions — the connected story

*How ARE's design evolved, told as one story for engineers who want the reasoning
behind each decision, not just the result. It is the narrative of one problem leading to the next
decision, each with the evidence that justified it and the cost it accepted. The
[ADRs](adr/) are the terse formal record of each decision in isolation; this document
is the **through-line** that connects them. Numbers are cited from
[FINAL_METRICS_SUMMARY](metrics/FINAL_METRICS_SUMMARY.md).*

---

## Chapter 0 — the failure that started it, and the first lesson

The project began with a single bad data point: a user typed *"I want to make
a trip to New York,"* and the copilot answered **"Policy is silent on this."** The
logged cause was a top cosine similarity of **0.0726**, below a hand-tuned grounding
gate of **0.20** — recorded in the companion
[research report](../RAG_RESEARCH_REPORT.md) and the archived
[RAG masterplan](../archive/RAG_MASTERPLAN.md) (`RAG_RESEARCH_REPORT.md:26,133`;
`archive/RAG_MASTERPLAN.md:23`). A travel-&-expense copilot
that abstains on *"a trip to New York"* fails at its basic job.

**The first lesson arrived while fixing it.** The original corpus was a single
hand-authored policy split into **8 clauses** — far too thin to retrieve against. When
the corpus was rebuilt into a real 208-unit multi-document body (R0; 219 units today), that very query
*stopped* abstaining: its measured max-dense similarity on the new corpus is **0.4138**,
and it now **grounds** (`R1-T3_T5_asbuilt.md:164–167`). The `0.0726` had been an
artifact of the deleted 8-clause stub. Rather than quietly keep citing a number that no
longer held, we retired it — and looked at what the *real* baseline showed.

It showed the deeper problem the symptom had been pointing at all along: on the
208-unit corpus, that query's gold units rank **r33–r184 — every one outside the
top-10** (`R0-T3_T6_asbuilt.md:93`). Dense-only retrieval buried the right answers.
Baseline recall@10 was **0.727**, and the `trip_planning` slice sat at recall **0.133**
(`R0-T3_T6_asbuilt.md:55`; after hybrid retrieval it is 0.232 —
[per-query-type table](metrics/FINAL_METRICS_SUMMARY.md#per-query-type-hybrid-local-path-k10)). The
gate was never the whole story; *retrieval quality* was. That reframing — symptom
(a mis-scaled gate) versus root cause (buried gold) — set the agenda for everything
that followed, and it foreshadowed the calibration bug we would hit much later.

---

## Chapter 1 — dense-only leaves recall on the table → hybrid retrieval

**Problem.** Dense embeddings match paraphrase but blur the exact tokens enterprise
policy hinges on — clause ids, dollar caps, city-tier names. The buried-gold ranks
above are the direct evidence: semantics alone could not surface the lexically-specific
units.

**Decision.** Add a **sparse lexical path** (PostgreSQL `tsvector` / `ts_rank_cd`, with
the query broadened from AND to OR so intent-mismatched utterances still match on any
term) and run it alongside dense retrieval. Reuse PostgreSQL — no second datastore.
Formal record: [ADR-001](adr/ADR-001-hybrid-retrieval.md).

**Evidence.** recall@10 **0.727 → 0.819** (+0.092) on the deterministic path, with
coverage and abstention held byte-identical (`R1-T3_T5_asbuilt.md:56,61`). The
lexically-matchable NY units lifted into range.

**Tradeoff accepted.** Two retrieval paths to maintain, and the broad OR-match *dilutes
precision* — it pulls in loosely-related units. We accepted that debt deliberately,
because the next decision was built to pay it down.

---

## Chapter 2 — fusing two incomparable score scales → RRF

**Problem.** Hybrid retrieval produces two ranked lists whose scores live on
incomparable scales (a dense cosine 0.4 and a sparse `ts_rank_cd` 0.08 cannot be added
or averaged). Weighted fusion would mean normalizing two shifting distributions and
tuning a weight per corpus — brittle.

**Decision.** Fuse by **Reciprocal Rank Fusion** — rank-only, one constant `k = 60`, no
per-corpus tuning — with a *reproducible* tie-break (`clause_ref`, never a per-seed
UUID). Formal record: [ADR-002](adr/ADR-002-reciprocal-rank-fusion.md).

**Evidence.** RRF *is* the +0.092 recall lift above; just as important, its determinism
is what makes the fused output byte-identical run-to-run, which is what lets CI gate
retrieval at all.

**Tradeoff accepted.** RRF throws away score *magnitude* — a dense hit that is
overwhelmingly close and one that barely clears threshold contribute equally at the same
rank. That precision loss is the exact thing Chapter 3 recovers.

---

## Chapter 3 — recall without precision → cross-encoder reranking

**Problem.** After Chapters 1–2 the right clauses were usually *in* the candidate set,
but not reliably at the *top* — RRF fuses blind to content, and OR-sparse added noise.
For a policy answer, the top few results are all the model sees.

**Decision.** Add a **cross-encoder** second stage: cheap hybrid retrieval proposes the
top 50, a model that reads query and passage *together* (`bge-reranker-v2-m3`) rescores
them into a high-precision top-k, in one batched call — behind a swappable
`RerankProvider` so CI runs a deterministic stub while production calls a real service,
and always **fail-safe** to RRF order on any error. Formal record and the provider
journey (Cohere → HuggingFace → Novita): [ADR-003](adr/ADR-003-cross-encoder-reranking.md).

**Evidence.** On the keyed path, nDCG@5 **0.658 → 0.873** (+0.215) and recall@10
**0.819 → 0.959** (`R2-T1_T2_asbuilt.md:211,214`). The precision debt from Chapters 1–2
was paid back with interest.

**Tradeoff accepted.** The reranker is a network hop — the dominant *retrieval* cost,
rate-limited, and (on the free tier we started with) slow. And because the CI stub is
RRF-anchored, the cross-encoder's lift is a **keyed-path** result, not a CI number — we
say so plainly rather than quoting 0.959 as if CI proved it.

---

## Chapter 4 — a magic number can't be trusted → a calibrated gate

**Problem.** The abstention gate was still the original hand-tuned cosine `0.20` — a
number with no error guarantee, brittle across query types, and reading the *weaker*
signal (raw cosine) when a better one (the reranker score) now existed. "Trustworthy
abstention" cannot rest on a magic number.

**Decision.** Replace it with a **data-derived** threshold. Calibrate the reranker
score into a probability with **Platt scaling**, then choose the threshold from a
**risk-coverage sweep** at a target error rate — *not* conformal prediction, which we
researched, considered as a co-equal option, and deliberately did not build. Formal
record, with conformal recorded as the considered alternative:
[ADR-004](adr/ADR-004-confidence-calibration.md).

**Evidence.** The reranker score is a materially better abstention signal than cosine:
AURC **0.047 vs 0.088** on the keyed set (≈46% lower; lower is better), and the R0
dense baseline's AURC was 0.101. The shipped operating point τ\* = **0.039299** answers
74% of queries at ~4.3% risk — a *stated* precision floor, not a guess
(`R3-T1_T2_asbuilt.md:78,81`).

**Tradeoff accepted.** τ\* is selected in-sample, so the honest headline is the
threshold-*independent* AURC comparison, not the in-sample τ\*. And — the decision that
would come back to bite us — the calibrated scale (~0.039) is nowhere near the cosine
scale (~0.20–0.70). That scale gap was a latent hazard. It detonated in Chapter 7.

---

## Chapter 5 — an answer you can't check isn't grounded → the trust surface

**Problem.** Even a correct answer is untrustworthy in an enterprise setting if the user
can't see *why*. And a confidence number on the raw calibrated scale (~0.04) is
meaningless to a human.

**Decision.** Enforce **document-qualified citations** end to end — every answer
sentence must copy a marker like `[T&E Policy §AIR-01]`, and any citation whose ref
isn't in the retrieved set is dropped so the model cannot fabricate a source — and
render a **calibrated confidence** the user can read (a τ-anchored normalization, floored
so a grounded answer never shows red), plus a calm escalation path on abstention.

**Evidence.** Citations are verified structurally (strict ref-matching) and re-checked by
an independent NLI entailment gate (threshold 0.55). This is also what made faithfulness
*measurable* later.

**Tradeoff accepted.** A per-sentence citation contract constrains generation (shorter,
stricter answers) and adds gates that can push a borderline answer to `needs_review`
rather than force it through — the right failure direction for policy.

---

## Chapter 6 — a decision you can't audit didn't happen → one enriched write

**Problem.** Every policy answer is an auditable enterprise decision. Incremental
per-stage logging would scatter one decision across rows and risk orphan/partial records
when a later stage failed.

**Decision.** Write **exactly one** enriched `ai_decision_logs` row per decision,
inside the request transaction, carrying the whole "why": candidates
(`document_id`, `unit_id`, `fused_rank`, `rerank_score`), the gate
(`source`, `signal`, `threshold`, `tau_star`), and the citations + NLI result. Formal
record: [ADR-005](adr/ADR-005-single-audit-write.md).

**Evidence / tradeoff.** One row reconstructs the full decision and is atomic with the
request (a presented answer is always logged; a rolled-back one logs nothing). The cost
is no per-stage streaming trace — accepted, since the enriched row captures the same
evidence and per-stage timing lives in the latency profiler.

---

## The honest arc: debugging the evaluation itself

The strongest engineering signal in this project is not any single metric; it is that
the two hardest bugs were in **how the system measured itself**, and both were found,
root-caused, and fixed rather than smoothed over. (The technical mechanism of each is
the canonical account in
[RAG_ARCHITECTURE §Measurement](RAG_ARCHITECTURE.md#measurement--honest-evaluation);
here is the story of *why they were hard*.)

**The confidence-scale bug — green CI, zero live grounding.** After all six chapters,
the keyed system did something alarming: it grounded **0 of 145** answerable queries,
answering *nothing* — while **CI was fully green**. The cause was the scale hazard from
Chapter 4 coming due. A citation-contract floor was a fixed `0.70`, a value on the
*cosine* scale; applied to the *calibrated* signal (operating point ~0.039), it was
structurally unsatisfiable, so every grounded answer was false-blocked. CI never saw it
because CI runs the *cosine* local path, where 0.70 is reachable — the local and keyed
paths had **diverged**, and only the keyed path was broken. This is the kind of defect a
deterministic CI *cannot* catch by construction, and recognizing that was the whole
fix: make the threshold **scale-aware** (floor at τ\* on the calibrated path, keep 0.70
on cosine), restoring live grounding **0 → 113/145** (`R6_gate_and_judge_fix_asbuilt.md:17–42`).
(C7R later lowered the floor on the *local* reranker's cosine path only — 0.55 plus a
mandatory answer-vs-question relevance gate below 0.70, which also never grounds a leading
"Yes,"/"No," verdict or an answer that skips a figure in the question — after an
agent-level run showed 0/54 out-of-corpus questions grounded with the gate and 5 → 12/27
answerable grounded; see
`RAG_ARCHITECTURE.md` §Measurement (c).  A calibrated reranker that falls back to cosine
keeps 0.70.)

**The faithfulness-measurement defect — 0.775 was the judge, not the model.** With
grounding restored, faithfulness was finally measurable — and came back **0.775**. The
easy conclusion was "the model hallucinates in ~22% of answers." The uncomfortable, correct
conclusion was that the *judge* was wrong. The holistic LLM judge penalized
citation-attribution phrasing ("also supported by §FAQ-Q05") by string-matching those
labels against chunk text where labels never appear — flagging **verbatim-grounded**
answers as unfaithful. A statement-level judge that *excludes* attribution phrasing and
scores atomic claims, with the identical binary-per-answer aggregation (so it is
apples-to-apples), measured **0.991** on the same answers. The lone remaining flag is
itself a verified judge error — **zero genuine model hallucinations**
(`R6_gate_and_judge_fix_asbuilt.md:53–79`). Faithfulness rose 0.775 → 0.991 **without a
single change to generation**: it was a measurement fix. (That 0.991 @ 78% grounded is
now history: after C7R added a stricter per-sentence support gate, the latest keyed run
measures **0.981 @ 74% grounded** (108/145), still with no confirmed hallucinations —
[metrics §4](metrics/FINAL_METRICS_SUMMARY.md#4-faithfulness--always-paired-with-coverage-keyed-only).)

The discipline this demonstrates — distrusting your own green checkmarks, distinguishing
a local-path artifact from a production truth, and auditing the judge before blaming the
model — is the reason the final numbers can be believed. And it closes the loop opened in
Chapter 0: the project began by retiring a stale number (`0.0726`) and ended by refusing
to ship two more misleading ones.

---

## Where the numbers live

Every figure in this narrative is one lookup away in
[FINAL_METRICS_SUMMARY](metrics/FINAL_METRICS_SUMMARY.md), with its source artifact.
The decisions in formal form are the five [ADRs](adr/). The system those decisions
produced is [RAG_ARCHITECTURE](RAG_ARCHITECTURE.md).
