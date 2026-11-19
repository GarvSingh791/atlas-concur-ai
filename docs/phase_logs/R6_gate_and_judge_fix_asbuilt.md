# R6 — Gate-Calibration Fix + Faithfulness Judge Fix (as-built)

**Date:** 2026-07-03 (gate-calibration fix) → 2026-07-05 (faithfulness judge fix + this log)
**Author:** Garv Singh Saini
**Branch:** r6-gate-calibration-fix (off main@6d0cc46 = R6-T3+T4; uncommitted at writing)

Unblocks Phase R6: restores live grounding and measures faithfulness correctly. Two logical
fixes, committed together as one unit — "restore grounding, then measure it correctly."

## Result (clean)
- Live grounding: **0 → 113 / 145 (78% answer-rate)** — needs_review 31, silent 1.
- Faithfulness (on answered): **0.991** [113 judged], **zero genuine failures** — the single
  flagged answer is itself a judge error, so the true number is conservatively understated.
- eval-db BYTE-IDENTICAL: recall@10=0.819031, coverage=1.000000, abstention=0.019231, 48 passed.

## The arc (why two fixes, one root cause)
A single structural flaw — a 0.70-scale threshold applied to a Platt-calibrated signal whose
operating point is τ*=0.039299 (max observed ~0.25) — surfaced three times, and a fourth bug
lived in the measurement:
1. **R6-T2 (display):** the calibrated confidence rendered grounded answers as alarming low
   values — fixed at the display layer.
2. **This fix (decision path):** the SAME 0.70 threshold in validate_grounding's citation
   contract made grounding structurally unsatisfiable on the calibrated path → live
   grounded=0 (100% false-block), while CI stayed green because CI runs the COSINE local path
   where 0.70 is reachable. This is the local-vs-live gap a deterministic CI cannot catch.
3. **NLI (secondary):** once the confidence gate was fixed, the lexical MIN-agg NLI gate at
   0.70 became binding on ~22/49 faithful paraphrases (H1) — relaxed to 0.55.
4. **Measurement (the fourth twist):** faithfulness first measured 0.775 — but that was a
   JUDGE defect (label string-matching), not generation. Corrected → 0.991.
At no fork was a threshold lowered blindly; each was root-caused and fixed at the right layer.

## Gate-calibration fix (grounding 0 → 113)
### FORK-A — calibrated citation floor = τ* (scale-aware)
validate_grounding (citations.py:117) gated at DEFAULT_CITATION_THRESHOLD=0.70 on
retrieval.confidence — the calibrated Platt probability (operating point τ*=0.039299, max
~0.25). Structurally unsatisfiable → asserted=False on 100% of the diagnostic sample, sole
cause for 55%. This 0.70 predates R3's swap of the confidence signal from cosine to the
calibrated reranker score; the gate silently became unsatisfiable on the calibrated path.
Fix: `resolve_citation_threshold(confidence_source, tau_star)` — calibrated path floors at
τ* (READ from retrieval.tau_star, the operating point the answerability gate already trusts;
not a second magic number); cosine/local path keeps 0.70 (eval-db byte-identical; C7R
later set 0.55 + an answer-vs-question relevance gate on the LOCAL reranker's cosine path
only — see RAG_ARCHITECTURE.md §Measurement (c); other cosine fallbacks keep 0.70). Floor
computed at the policy_agent site (where retrieval.confidence_source/tau_star are in scope)
and passed via the existing reflect(threshold=) param; other reflect() callers untouched.
Safety now rests on citation-PRESENCE + critic + NLI. Live proof the gate still catches real
cases: ans-142 (0 citations) → needs_review, withheld by presence, not confidence.

### FORK-C — NLI 0.70 → 0.55, MIN retained
Recovers H1 faithful paraphrases in [0.55, 0.70) while keeping per-sentence accountability
(a 0-overlap sentence still blocks — verified). Deliberate, bounded relaxation of the
deterministic entailment bar; distinct from the scale fix. Lexical NLI stays negation-blind
(pre-existing).

## Faithfulness judge fix (0.775 → 0.991, measurement-only)
### The defect
The production _judge_faithful was a HOLISTIC single yes/no LLM call. It tripped on
citation-ATTRIBUTION meta-statements ("also supported by FAQ-Q05") by string-matching the
literal label inside chunk_text, where labels NEVER appear (structural metadata — same class
as the R5 NLI marker-stripping). The policy CONTENT was verbatim grounded in all flagged
answers → 0.775 UNDERSTATED real faithfulness.
### The fix (attribution-exclusion)
`_judge_faithful_decomposed`: split the answer into atomic substantive policy claims (amounts,
rules, procedures, eligibility, approvals); EXCLUDE citation-attribution meta-statements from
the claim set (they are not factual claims; judging them by label-string-match is a category
error) and instruct the per-claim judge to ignore labels when scoring support; fail-closed
parsing (validates shape, returns None on malformation — stricter than the old judge).
Aggregation UNCHANGED = binary-per-answer (all(claim.supported)) → apples-to-apples with the
0.775 baseline; the delta is judge-quality only. Claim-fraction (387/388 = 0.997) is additive
transparency, not gated.
### Measured (keyed eval-live-r5 on the fixed tree)
faithfulness 0.991 [113/113] · claim-fraction 0.997 [387/388] · disposition 113/31/1
(stable vs 111/33/1 — judge-only change) · citation_complete 0.949 · nli_entail_rate 0.889 ·
context_prec-MAP 0.779 · Novita 290 @ 17.3/min (≤30) · wall 1006s.
Genuine failures: ZERO. ans-052 is a judge error (invented a false baggage/seat-fees premise
for a vaccination-reimbursement query that restates INTL-05 verbatim, grounded mds=0.808) →
real faithfulness conservatively understated at 0.991.
### Floor
RAG_FAITHFULNESS_FLOOR 0.80 (placeholder) → **0.95** — below measured 0.991 with ~4 pt
headroom, and it actually catches a regression (0.90 sat 9 pt below measured and would not
trip on a real drop). Keyed manual regression guard; faithfulness is never CI-gated.

## Decisions (labeled)
| # | Decision | Label |
|---|---|---|
| A | calibrated citation floor = τ* (scale-aware via confidence_source) | APPROVED-FORK |
| C | NLI 0.70 → 0.55, MIN retained | APPROVED-FORK |
| — | statement-level decomposed judge + attribution-exclusion; binary aggregation unchanged | APPROVED-FORK |
| — | RAG_FAITHFULNESS_FLOOR → 0.95 (tightened from the FSR's 0.90) | APPROVED-FORK |
| — | no τ-anchored display transform exists (R6-T2 shows raw); computed floor never leaks to audit | CODE-WINS |

## Gates
- Live: grounding 0 → 113/145; faithfulness 0.991 (0 genuine failures).
- eval-db BYTE-IDENTICAL: recall@10=0.819031, coverage=1.000000, abstention=0.019231, nDCG@5
  =0.658420, AURC=0.084096 — 48 passed, test_r6t4_regression_floors PASS.
- Hermetic: 498 passed / 0 failed; test_r6_gate_calibration 12; R6-T2 seam 14 + R6-T3 audit
  11 green; single-grounded-write fence re-asserted (len(records)==1 on both paths).
- ruff/format clean (358); mypy clean (148); layering subprocess check green.
- git diff database/ EMPTY; ai_agents/ = gate fix only; config/settings = NLI value only;
  evals/ = judge + floor. Computed floor proven local-only (test F sentinel ≠ floor).

## R6-T5 handoff (this unblocks the writeup)
Faithfulness is now real, reproducible from the committed harness, and paired with coverage:
**0.991 faithful @ 78% grounded**. Document calibration as Platt scaling + risk-coverage/AURC
(τ*=0.039299) — NOT conformal (conformal was a researched option, not built). The debugging
arc above is the honest-evaluation centerpiece.

## Carried-forward
- **NLI 0.55 negation-blindness** (lexical, pre-existing) — not currently biting
  (nli_entail_rate 0.889); revisit only if a negation case surfaces.
- **Floor 0.95 is a manual keyed guard** — faithfulness is never on the deterministic CI
  build (no LLM judge on local); it runs on the keyed eval-live-r5 / workflow_dispatch.
- **R6-T5 public documentation** — the remaining Phase R6 deliverable.

## Process note
Gate fix: 4-lens adversarial review (Test D flips grounded↔needs_review at floor 0.70; Test C
lands genuinely in [0.55,0.70)). Judge fix: 5-pass verify + 2-lens adversarial (metric-shape
unchanged, improvement judge-only, ans-052 a judge error, floor honest) — all re-verified.
