# P4-T3 — Reflection / Critic

**Sub-phase:** P4-T3  
**Title:** Reflection / Critic  
**Date:** 2026-06-13  
**Status:** COMPLETE & VERIFIED  
**Author:** Garv Singh Saini

---

## Objective

Add the third guardrail: a bounded **generator→critic reflection loop** (FR-A3)
that checks a seed hypothesis against injected grounding facts before committing
any value — the mechanism that solves the "Paris paradox" (a receipt that reads
*Paris, France* is corrected to *Paris, Texas* because the itinerary shows a
Dallas-area trip). The loop is reusable and generic; no agent or pipeline
consumes it yet. Grounding is injected (not fetched live — that is P4-T4).

Gate: the Paris-paradox fixture must catch and revise the wrong-city hypothesis.

---

## What Was Built

### `ai_agents/guardrails/reflection.py` — the full reflection substrate

**`reflect()`** — bounded generator→critic loop (`DEFAULT_MAX_ROUNDS=3`) over
injected `Hypothesizer` + `Critic` protocols. Returns a `ReflectionResult`
carrying:

- `outcome` — the `database.models.ai_decision_log.ReflectionOutcome` enum
  (`passed | revised | rejected`), 1:1 with
  `ai_decision_logs.reflection_outcome`. Reused directly; not forked.
- `grounded` — the validated P4-T2 `GroundedResult` (value, citations,
  confidence, asserted). For `passed`/`revised`: `asserted=True`, ≥1 citation,
  `confidence >= DEFAULT_CITATION_THRESHOLD (0.7)`. For `rejected`:
  `asserted=False` (safe default — not presented to the user; deferred for
  clarification).
- `critique` — rationale of the deciding critique.
- `needs_review` — `True` when the LLM critic degraded or the loop rejected;
  signals the caller to flag the output for human review.
- `rounds` — number of generator→critic iterations consumed.

**`Hypothesizer` protocol** — `seed() → GroundedResult`; `regenerate(previous,
critique) → GroundedResult`. Callers inject their own strategy; the loop stays
fully generic.

**`Critic` protocol** — `__call__(hypothesis: GroundedResult) → Critique`.
Facts are bound into the critic, not passed to the loop.

**`Critique`** — frozen dataclass: `verdict` (`supported | contradicted |
unsupported`), `rationale`, optional `correction` (`GroundedResult` carrying
the revised value + citation for `contradicted`, or the citation-enriched
hypothesis for `supported`), `needs_review`.

**`GroundingCritic`** — reference critic; deterministic-first:

- **Stage 1 (no LLM)**: if `hypothesis.value` is a `LocationClaim`, matches
  its city (case-insensitive) against `facts.locations`. Consistent
  region/country → `supported` (with citation). Same city but conflicting
  region/country (e.g. `country="France"` vs `region="TX"`) → `contradicted`
  with a `correction` toward the grounded fact. No matching city → `unsupported`.
  This stage alone catches the Paris paradox.
- **Stage 2 (optional LLM)**: fires only when Stage 1 is inconclusive (value
  is not a `LocationClaim`). If `provider is None` or raises `ProviderError`,
  degrades gracefully: returns `unsupported + needs_review=True` (NFR-R1).
  Never crashes; never guesses.

**`LocationClaim`**, **`LocationFact`**, **`GroundingFacts`** — frozen dataclasses
for injected location context. P4-T4 (grounding tool layer) will populate these
from live sources.

**Pure loop — no DB writes.** `reflect()` returns the `ReflectionResult`;
callers (P4-T9 recommendation, P4-T11 gap-fill) map `result.outcome` →
`DecisionRecord.reflection_outcome` and call `write_decision_log` inside their
own transaction:

```python
rec = DecisionRecord(
    ...
    reflection_outcome=result.outcome,
    confidence=result.grounded.confidence,
    output_summary=result.grounded.to_dict(),
)
write_decision_log(session, context=ctx, record=rec)
```

### `ai_agents/guardrails/__init__.py` — extended exports

Added `reflect`, `ReflectionResult`, `Critique`, `CriticVerdict`, `Hypothesizer`,
`Critic`, `GroundingCritic`, `GroundingFacts`, `LocationFact`, `LocationClaim`,
`DEFAULT_MAX_ROUNDS`, and re-exported `ReflectionOutcome` (from
`database.models`) for caller ergonomics.

### `ai_agents/tests/test_reflection_loop.py` — 32 hermetic tests

| Group | Tests | What is proven |
|-------|-------|----------------|
| Paris paradox | 5 | `France` hypothesis + TX facts → `revised`, `asserted=True`, ≥1 citation, `validate_grounding` passes; **never a silent `passed`** |
| Correct hypothesis passes | 4 | `Paris/TX` + TX facts → `passed`; no over-correction; `asserted=True`; ≥1 citation |
| Unsupportable (no facts) | 3 | Unknown city + empty `GroundingFacts` → `rejected`, `asserted=False`, `needs_review=True` |
| ProviderError degrades | 3 | Non-location value + `_RaisingProvider` → `rejected`, `needs_review=True`; no crash |
| max\_rounds exhaustion | 4 | `_IgnorantHypothesizer` (ignores correction) → stops at `max_rounds`, `outcome=rejected`, `rounds==max_rounds`; `max_rounds=0` → immediate reject |
| Outcome maps to DB enum | 4 | `result.outcome is DbReflectionOutcome.*` for all three values; enum completeness |
| Deterministic critic | 5 | `GroundingCritic(provider=None)` catches contradiction (Stage 1), supports correct claim, unsupported absent city, correction carries citation, no-provider + non-location → `needs_review` |
| `validate_grounding` contract | 3 | Revised result satisfies full contract (`asserted=True`, ≥1 citation, `confidence≥0.7`); convenience properties delegate correctly |
| Constants | 1 | `DEFAULT_MAX_ROUNDS == 3` |

### `ai_agents/tests/test_no_backend_import.py` — updated subprocess proof

Added `import ai_agents.guardrails.reflection` to the subprocess import script
so the layering-fence check covers the new module.

---

## Key Decisions

### (a) Reuse P4-T2 types — no forks

`Citation`, `GroundedResult`, `validate_grounding`, `DEFAULT_CITATION_THRESHOLD`,
and `ReflectionOutcome` are imported from their canonical locations; none are
redefined. The `ReflectionOutcome` enum is re-exported through `guardrails/__init__`
for caller ergonomics but remains the exact same object (`is` identity preserved).

### (b) `Critique.correction` carries the citation on the "supported" path

When Stage 1 finds a supporting fact, it builds a citation-enriched
`GroundedResult` and returns it in `correction` (even for `verdict="supported"`).
The loop uses `c.correction if c.correction is not None else hypothesis` when
calling `validate_grounding`, ensuring the supporting citation is present and
`asserted=True` is reachable. Without this, the original hypothesis (no
citations) would fail the grounding contract and produce a false `rejected`.

### (c) `max_rounds` cap prevents infinite spin

A hypothesizer that never improves (always regenerates to the same wrong
answer) will exhaust `max_rounds` and return `rejected/defer`. No escape hatch;
the loop is strictly bounded.

### (d) Rejection always sets `asserted=False`

The `_rejected()` helper enforces this unconditionally. A rejected result is
not presented to the user; it defers for clarification or falls back to
deterministic output (TECHNICAL_DESIGN "Reflection rejection → safe default").

### (e) Pure loop — return, don't write

The loop never opens a DB session. The decision-log seam is documented in the
module docstring with an explicit code snippet for P4-T9/T11 callers. This
keeps the loop hermetically testable without Postgres.

---

## Files Modified / Created

```
ai_agents/guardrails/reflection.py          (new)
ai_agents/guardrails/__init__.py            (extended)
ai_agents/tests/test_reflection_loop.py    (new)
ai_agents/tests/test_no_backend_import.py  (updated — added reflection import)
docs/phase_logs/P4-T3_reflection_critic.md (this file)
```

No new DB table, column, or migration. No `config/constants/` change.

---

## Validation

Gate condition met: the Paris-paradox fixture is caught and revised.

| Check | Result |
|-------|--------|
| pytest (full suite) | **151 passed, 171 skipped** (live-DB/storage self-skip) |
| ai\_agents/ hermetic tests | **76 passed, 13 skipped** (13 live-DB tests self-skip) |
| test\_reflection\_loop.py | **32 passed** |
| Paris fixture | `France` → `revised`, `asserted=True`, ≥1 citation, `validate_grounding` passes, never `passed` |
| Correct hypothesis | `TX` → `passed`, not over-corrected |
| Unsupportable | `rejected`, `asserted=False`, `needs_review=True` |
| ProviderError | `rejected`, `needs_review=True`, no crash |
| max\_rounds exhaustion | Stops at 3; `rejected` |
| ruff check | Clean |
| ruff format --check | Clean |
| mypy ai\_agents/ | **17 files, 0 issues** (locally) |
| Layering fence grep | Zero violations in new files (`backend`, `fastapi`, `AppError` absent) |
| Subprocess isolation test | `ai_agents.guardrails.reflection` imports without pulling `backend.*` |
| `/health` endpoint | 200 OK |
| Alembic autogenerate drift | Empty — no model or migration change |

---

## Known Gap (flagged — separate task, not P4-T3)

The CI mypy target (`Makefile:22`) is `mypy backend config`. The `ai_agents/`
and `services/` packages are **not type-checked in CI**. Verified locally only
(`mypy ai_agents/ → 17 files, 0 issues`). Recommend a separate near-term
tooling task to extend the CI ruff/mypy target to cover `ai_agents services`.

---

## QA Findings (found and fixed during implementation)

1. **"supported"-path citation dropped**: the loop validated `hypothesis`
   directly on a `supported` verdict, discarding the citation-enriched
   `Critique.correction` built by the deterministic critic. Fixed: use
   `c.correction if c.correction is not None else hypothesis` so the supporting
   citation is preserved and `asserted=True` is reachable.
2. **ruff I001** (import sort) in `guardrails/__init__.py` — auto-fixed.
3. **ruff F401** (unused `pytest`, `Hypothesizer`, `ReflectionResult`) in the
   test file — auto-fixed.
4. **ruff E501** in the Stage 2 LLM prompt f-string — split across two lines.

---

## Deviations

None.

---

## Next

**P4-T4** — Grounding tool layer: the live sources (itinerary, calendar,
inventory, policy) that agents fetch and bind into `GroundingFacts` for the
critic. The `ToolSpec`/`ToolCall` seam from P4-T1 meets the reflection critic
here.
