# R3-T3 + R3-T4 + R3-T5 — Confidence/Rationale Fix · Blameless Abstention UX · Eval Contract (as-built)

**Branch:** `r3-ux-eval` (off `main` post-R3-T1+T2 squash) · **Status:** complete, UNCOMMITTED (committed in a separate step)
**Date:** 2026-06-29 · Built in stages (implement → adversarial review → parallel tests → parallel verify; 15 review passes) + reconcile & fixes.

## Goal

Close Phase R3: (T3) fix the confidence/"why this answer" mismatch and source grounded
confidence from the calibrated score; (T4) ship a blameless, deterministic (LLM-free)
abstention payload — escalation contact + nearest-topic suggestion; (T5) prove the
abstention contract — precision ≥ 95% (hard), recall=0.886 documented honestly, AURC
improved — all additive, no gate/threshold/migration change.

## Reconcile findings (CODE WON over the task spec's premises)

- **🚩 F-1 — the confidence bug was ALREADY FIXED on `main`.** The "40% confidence + Resolved
  with LLM reasoning on a silent answer" symptom is not reproducible: the silent path already
  set `confidence=None` (`retrieval.py:211,249`) and already guarded the LLM
  (`policy_agent.py:81`, `model_provider=None`). "40%" can't even come from `_confidence()`
  (floors at 0.5). → R3-T3(a) needed **no production code**, only a **regression test** that
  *locks* I_R3_6 (zero LLM calls + `confidence=None` on silent) plus the new user-facing
  rationale. The pre-existing `_RaisingProvider` Gate-4 tests already partly covered this; we
  added explicit R3 payload tests.
- **🚩 F-2 — citations were ALREADY document-qualified.** `Citation(ref=clause_ref,
  source=policy_name)` (`retrieval.py:284–291`). The combined `"§x (Policy)"` *string* is a
  frontend concern (R6-T2). → R3-T3(c) = confirm + add a `policy_name or "Policy Document"`
  safety fallback (done in both the citation builder and the near-topic builder).
- **🚩 F-3 — gate-aligned confidence-source predicate (design correction).** The task spec's
  literal "`calibrated_score is not None` → use it" is wrong here: the **local** provider also
  produces a non-None `calibrated_score` (Platt params exist for `local` in
  `rerank_calibration.json`), so the literal rule would mislabel local as `"calibrated"` and
  fail V3. We key off the **gate basis** `tau_star is not None and top_calibrated is not None`
  (the same predicate the gate uses), which makes local→`"cosine"`, HF→`"calibrated"` and
  **directly fixes deferred R3-T2 QA-finding #3**.
- **F-4 — live path is SSE streaming**, not a plain `/policy/query` DTO. Additive data rides
  `AgentResult.output` (generic dict, forwarded verbatim by `streaming.py:62`), so **no
  streaming change was needed**. The 2 optional `ConversationMessageResponse` fields satisfy
  the explicit DTO requirement (I_R3_9).
- **F-5 — `org_name` not in Settings.** `ORGANIZATION_NAME` existed only in `.env.example`
  (`Acme Corp`); we wired it into `Settings` (default `"Acme Corp"`, fallback `"your
  company's"`). Escalation defaults are **generic** (`"your team lead"` / `"Travel Desk"`) per
  the task-spec mandate; the Helios `CONT-01/02` emails (`travel@/expenses@/compliance@…`) are
  referenced in the `.env.example` comment only, never hardcoded.
- **F-6 — R3-T5 harness already exists.** `evals/calibration/test_fit_abstention_threshold.py`
  already computes/prints precision-floor, abstention recall, leakers, AURC, NY. We added the
  per-leaker calibrated-confidence print (additive) and a **new keyless artifact-contract
  test** that hard-asserts the contract from the committed JSON — giving CI-safe V14/V15/V17
  **without re-fitting the HF artifact** (honors F3 / threshold-hf NOT run).
- **F-7 — Postgres CLI tools absent, but the DB server is live.** `psql`/`pg_isready` are
  not installed, but the Postgres server (localhost:5432) is reachable via psycopg2. The
  DB-marked gates therefore RAN green in the commit-prep run (see Step A0): `make eval-db` = **47 passed**
  live; `make threshold-local` reproduced local τ\*=0.220598 (not promoted → cosine fallback)
  and left `abstention_threshold.json` **byte-identical** (HF entry preserved — F3). The new
  tests live in `ai_agents/tests/` (not `backend/tests/eval/`), so the 47-count is structurally
  untouched; the cosine eval is isolated from the agent layer by design.

## What shipped

| Artifact | Path | Notes |
|---|---|---|
| Near-topic type + DTO fields + gate-aligned confidence | `ai_agents/rag/retrieval.py` | `NearTopicSuggestion` frozen dataclass; additive `confidence_source` / `near_topic_suggestion` on `GroundedRetrieval` + `to_dict()`; near-topic built in the below-threshold silent branch (`max_dense>0.10`, `[:200]`, fallbacks); citation `source` fallback; **gate-aligned** confidence source (`tau_star` present → `calibrated`, else `cosine`). |
| Blameless abstention payload | `ai_agents/agents/policy_agent.py` | `from config.settings import get_settings`; silent `AgentResult.output` now carries `rationale` + `escalation{message,secondary}` + `near_topic` (LLM-free); grounded output adds `confidence` + `confidence_source`. Existing silent keys retained. |
| Escalation + org settings | `config/settings/base.py` | `ORGANIZATION_NAME`, `ESCALATION_CONTACT_PRIMARY`, `ESCALATION_CONTACT_SECONDARY` (str defaults; no new required env var). |
| Env doc | `.env.example` | escalation block + Helios `CONT-01/02` fixture reference comment. |
| Additive backend DTO | `backend/app/modules/ai/schemas.py` | `ConversationMessageResponse` += optional `confidence_source`, `near_topic_suggestion` (None-defaulted). |
| Leaker-confidence print | `evals/calibration/test_fit_abstention_threshold.py` | additive print of each leaker's calibrated score (~4–8%); no computed value/assert/τ\* touched. |
| Abstention-payload tests | `ai_agents/tests/test_r3_abstention_payload.py` | 12 hermetic tests; `_RaisingProvider` spy proves **zero LLM calls** on silent; rationale/escalation/near-topic/confidence=None. |
| Confidence-source + citation tests | `ai_agents/tests/test_r3_confidence_source.py` | 4 hermetic tests; calibrated vs cosine (incl. the local-has-calibrated-score trap), doc-qualified citation + `"Policy Document"` fallback. |
| Near-topic tests | `ai_agents/tests/test_r3_near_topic.py` | 5 hermetic tests; soft floor (0.15>0.10 vs 0.05≤0.10), no-chunks→None, `[:200]` slice, policy-name fallback. |
| Keyless abstention-contract test | `ai_agents/tests/test_r3_abstention_contract.py` | 1 test; hard-asserts `1−risk_at_tau≥0.95`, `ny_canary_answered is True`, `aurc_reranker<aurc_cosine`, `promoted is True`; soft-prints recall=0.886 framing (no `assert recall==1.0`). |
| Stale shape test updated | `ai_agents/tests/test_rag_retrieval.py` | `test_to_dict_shape` `expected_keys` += the 2 additive R3 keys (additive DTO contract). |

## Verification (this environment)

- **Ruff:** `All checks passed!` on all touched + new files.
- **mypy** (`backend config evals`): `Success: no issues found in 144 source files` — clean, no new errors.
- **Hermetic pytest** (R3 + policy_agent + retrieval + calibrated-gate + no-backend-import): **88 passed, 0 failed** (the regression in `test_to_dict_shape` was an additive-key update, now green).
- **`make eval-db`:** **47 passed** live (commit-prep step A0a); count unchanged (17+11+13+6); new tests are in `ai_agents/tests/`, so the eval-db tier is untouched.
- **`make threshold-local`:** ran green (commit-prep step A0b) — local τ\*=0.220598 reproduced, NOT promoted (NY abstains on local → cosine), `abstention_threshold.json` byte-identical (HF τ\*=0.039299 preserved, F3); leaker-confidence print verified (una-037=0.2223, una-033=0.2278, una-035=0.2277 on the local gate).
- **CI-hygiene:** `ruff format` applied to the 2 new/edited files CI gates (`test_r3_abstention_contract.py`, `test_fit_abstention_threshold.py`); repo now 335/335 format-clean.
- **Layering (I_R3_5):** `test_no_backend_import.py` passes; `grep "from backend|import backend"` over the edited ai_agents files = empty; no backend modules leak into `sys.modules`.
- **Fences:** `git status` confirms `abstention_threshold.json`, `rerank_calibration.json`, migrations, fusion/SQL, endpoints, and dependency manifests are **untouched**. `make threshold-hf` NOT run (F3).
- **Tier-3 numbers** (from the committed `ai_agents/rag/abstention_threshold.json` HF block; threshold-hf not re-run):
  - precision-on-answered = `1 − risk_at_tau` = `1 − 0.043478` = **0.9565 ≥ 0.95** ✅ (V14)
  - `ny_canary_answered = true` ✅ (V15)
  - AURC reranker **0.046982** < cosine **0.087511** (−46%) ✅ (V17)
  - coverage@τ\* = 0.741935; τ\* = 0.039299 (promoted)
  - abstention recall = **39/44 = 0.886** (soft); 5 leakers `una-037/039/035/034/038` at calibrated confidence just above τ\*=0.039 (~4–8%). recall=1.0 would collapse coverage; the precision floor is the chosen tradeoff.

## Invariants

I_R3_1 precision≥95% ✅ (artifact-contract) · I_R3_2 NY answered ✅ · I_R3_3 fail-safe cosine
(predicate unchanged) ✅ · I_R3_4 `retrieve()` signature byte-identical (body-only edits) ✅ ·
I_R3_5 layering ✅ · I_R3_6 silent: confidence=None + 0 LLM (spy) ✅ · I_R3_7 grounded
confidence calibrated(HF)/cosine(local) ✅ · I_R3_8 escalation always + near-topic on
near-miss ✅ · I_R3_9 DTO additive-only ✅.

## QA findings flagged for follow-up

1. **A0 ran GREEN live (commit-prep run).** Postgres server is reachable (only the `psql`/`pg_isready`
   CLI tools are absent). `make eval-db` = 47 passed; `make threshold-local` reproduced local
   τ\* and left the artifact byte-identical (F3). V8/V16 are closed live, not deferred.
2. **mypy on `ai_agents/tests/`** (NOT a CI gate — CI checks `backend config evals`, which is
   clean): the 3 new R3 hermetic test files use `-> object` helper annotations → 23 mypy
   `attr-defined` notes under a broader `mypy … ai_agents` run. Outside CI scope and tests pass
   at runtime; left as-is per the "modify only narrow CI-hygiene" rule. Trivial follow-up if
   desired: annotate the `_run`/fake helpers with `AgentResult`/`GroundedRetrieval` return types.
3. **HF leaker confidences** reported from the committed artifact + R3-T2 record (F3 forbids
   re-fitting). Re-run `make threshold-hf` separately only if live re-verification is desired
   (it rewrites the artifact). (Local-gate leakers from A0b: una-037/033/035 — different set,
   cosine scale ~0.22; the promoted HF gate's leakers are una-037/039/035/034/038.)
4. **`schemas.py` field placement:** the 2 new optional fields sit between `citations` and
   `sequence` (declaration order only; non-breaking for Pydantic `from_attributes`).

## R3-T5 results table (consolidated — committed artifact + R3-T2 record)

| Metric | Value | Gate |
|---|---|---|
| Precision on answered (1 − risk@τ\*) | **95.65%** (1 − 0.043478) | ≥ 95% — **HARD PASS** |
| Abstention recall | **39/44 = 0.886** | soft (honest report) |
| Leakers (unanswerable that answered) | una-037 / una-039 / una-035 / una-034 / una-038 | ~4–8% calibrated confidence (just above τ\*=0.039) |
| Coverage at τ\* (HF gate) | 74.19% | reported |
| AURC reranker vs cosine | **0.046982 vs 0.087511** (−46%) | reranker WINS |
| NY (ans-142) | **ANSWERED** (ny_canary_answered=true) | coverage canary — PASS |

Honest recall framing: recall=1.0 would require a stricter τ\* that collapses coverage;
the 95% precision floor is the deliberate selective-prediction tradeoff. `make threshold-hf`
was NOT re-run (F3 — the committed artifact is the source of these HF numbers).

## Section 10 gate roll-call (R3 Part 2 — each individually)

**Tier 1 (terminal-provable)**
1. **Lint clean** — ✅ `ruff check` passes on all touched/new files.
2. **Types clean** — ✅ `mypy backend config evals` = "no issues in 144 source files" (ai_agents/services excluded per CI scope).
3. **Unit tests pass** — ✅ 88 hermetic pass (new R3 suites + existing policy_agent/retrieval/calibrated-gate; no regressions).
4. **Eval runs deterministically** — ✅ `make eval-db` = **47 passed** live (commit-prep step A0a); `make threshold-local` green, artifact byte-identical.
5. **Retrieval metric** — N/A this phase (R3 hardens abstention, not retrieval; recall/nDCG carried from R1/R2).
6. **Abstention metric (R3+)** — ✅ precision 95.65% ≥ 95%; coverage 74.19%; AURC reranker < cosine (HF). recall 39/44 soft.
7. **Trip-to-NY regression** — ✅ ny_canary_answered=true (artifact, HF promoted gate); confirmed live via A0a (47 passed includes the corpus NY regression). On the local gate NY abstains by design → local not promoted (cosine fallback).
8. **Latency within budget** — N/A this phase (no change to retrieval timing path).
9. **Idempotent re-index** — N/A this phase (no embedding/seed change).
10. **Secret scan clean** — ✅ five-check scan clean (see commit-prep; Pydantic defaults + .env.example comments are known false positives).
11. **Migrations apply/downgrade** — N/A this phase (F4 — no schema change).

**Tier 2 (manual/observed)**
12. **"Try to break it" adversarial pass** — ✅ 15-pass adversarial review, 0 invariant/fence violations; out-of-scope queries return the blameless abstention payload (no world-knowledge leak).
13. **Frontend trust surface** — N/A this phase (R6-T2); additive DTO fields (confidence_source, near_topic_suggestion) prepared for the FE render.
14. **Audit completeness** — ✅ unchanged discipline: silent path writes a single decision log (model_provider=None); grounded path one row.

**Caveat:** trustworthy *calibrated* abstention is live only under `RERANK_PROVIDER=huggingface`
(τ\*=0.039299 promoted); `local` is not promoted (τ=null) and uses the proven R2 cosine gate.
Set `RERANK_PROVIDER=huggingface` in the production environment for the calibrated gate.
