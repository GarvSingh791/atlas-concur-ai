# P4-T4 — Tool Layer (Grounding)

**Sub-phase:** P4-T4  
**Title:** Tool Layer (Grounding)  
**Date:** 2026-06-13  
**Status:** COMPLETE & VERIFIED  
**Author:** Garv Singh Saini

---

## Objective

Build the five read-only grounding tools agents call to "fetch the truth rather
than hallucinate it" (FR-A2 — Tool-Use pattern from research.md). Each tool
receives dependencies injected and returns typed plain-data results carrying
citation-able source references. No agent, orchestrator, or registry consumes
them yet; the gate is per-tool unit tests against the P3-T5 seed.

This is **file-order 22** in the build sequence:  
`services/…` (20) → `ai_agents/guardrails/…` (21) → **`ai_agents/tools/…`** (22) →
`ai_agents/orchestrator/…` (23).

---

## What Was Built

### `ai_agents/tools/base.py`

- `ToolError(GuardrailError)` — tools raise this, never backend `AppError`.
  `code="TOOL_ERROR"` (overridden per tool with `code="INVENTORY_TOOL"` etc.).
- `GroundingTool` — a `@runtime_checkable` `Protocol` for type-checker use only;
  not inherited. Registry routing is P4-T7.

### `ai_agents/tools/inventory.py` — `InventoryTool` (hermetic)

Wraps `MockInventoryProvider` (P4-T1). Dispatches to `search_flights` or
`search_hotels` by `segment_type` (the provider exposes two typed methods, not a
generic `search(segment_type=)`). Returns `InventoryResult(options=tuple[InventoryOption])`.

- **No DB, no tenant filter** — the dataset is global mock data.
- `AgentContext` accepted for signature uniformity but not used.
- `option_id` (Flight.flight_id / Hotel.hotel_id) → `Citation(kind=tool_result,
  ref=option_id, source="mock_inventory")`.
- Empty match → empty tuple (never fabricated options).

### `ai_agents/tools/policy_eval.py` — `PolicyEvalTool` (canonical evaluator)

The **single source of truth** for rule evaluation. The backend
`/policy/evaluate` endpoint (P4-T13) will **delegate INTO this tool**, not the
reverse. No LLM.

**Rule selection:** `SELECT policy_rules JOIN policies WHERE
policies.organization_id = :org AND policy_rules.status = 'active'`.
The JOIN is mandatory — `policy_rules` has no `organization_id` column.

**Applicability:** a rule applies when `rule.applies_to == item.item_type` OR
`rule.applies_to == PolicyAppliesTo.all`.

**Six rule-type handlers:**

| rule_type | clause | Logic |
|---|---|---|
| `cabin_class` | AIR-CABIN | Resolve allowed classes from `duration_hours` tier; compare `cabin_class`. |
| `amount_cap` | HOTEL-CAP | Resolve tier from `city` (comma-normalised, case-insensitive Tier-1 match); compare `amount_usd` ≤ cap. Conservative fallback: `domestic` cap. |
| `per_diem` | MEALS-PD | Pick rate by `region`; group-meal headcount adds `requires_approver`. |
| `preferred_supplier` | AIR-PREF (blocking) / HOTEL-PREF (advisory) | AIR-PREF: non-preferred → `in_policy=False`; HOTEL-PREF ("should") → advisory, never blocking. |
| `category_allow` | GROUND-CLASS | Allowed → IN; excluded → OUT+VP; conditional (Intermediate) → default OUT, condition unverifiable from item. |
| `receipt_required` | RECEIPT-REQ | Required when `amount ≥ threshold` or `category in always_required`. For prospective options → informational only. |

**Return:** `PolicyVerdict(item, in_policy=all(verdicts), verdicts=tuple[RuleVerdict])`.
Each `RuleVerdict` carries `source_clause_ref` from the DB row →
`Citation(kind=clause_ref, ref=source_clause_ref, source="policy_rules")`.

**SUSTAIN clause → no verdict:** structural guarantee. No `policy_rules` row has
`source_clause_ref="SUSTAIN"` (SUSTAIN is intentionally rule-less). No
special-case code required.

### `ai_agents/tools/policy_retrieval.py` — `PolicyRetrievalTool` (RAG)

Embeds the query via the injected `EmbeddingsProvider` → pgvector cosine ANN
over org-scoped `policy_embeddings` → top-k `(chunk_text, clause_ref, similarity)`.

**Org-scoping:** `policy_embeddings` has no `organization_id` —
`JOIN policies ON p.id = pe.policy_id WHERE p.organization_id = :org`.

**Query:** uses `<=>` (cosine distance) with `ORDER BY` on the distance
expression (not the aliased similarity) so the planner uses
`ix_policy_embeddings_embedding_hnsw`.

**Strict top-k-only boundary:** NO threshold, NO "policy is silent" decision
(P4-T6), NO answer assembly, NO re-index (P4-T5).

`clause_ref` → `Citation(kind=clause_ref, ref=clause_ref, detail=chunk_text,
source=policy_name)`.

### `ai_agents/tools/profile.py` — `ProfileTool`

Reads the context user's `users` row + recent trips (both org + user scoped).
**NULL-safe:** `preferences = row.preferences or {}` — all seed users have
`preferences=NULL`; the tool never returns `None`.

Raises `ToolError` if `context.user_id is None` (system principal has no
profile).

### `ai_agents/tools/itinerary_calendar.py` — `ItineraryCalendarTool`

Reads trips + trip_segments + calendar_events, all org + user scoped.
`TripSegment` has no `organization_id` or `user_id` column — scoped
**transitively** via `TripSegment.trip_id IN (scoped trip ids)`. Never queried
standalone.

Raises `ToolError` if `context.user_id is None`.

`segment_id` → `Citation(kind=source_field, ref="trip_segments.id", source="trips")`.  
`event_id` → `Citation(kind=source_field, ref="calendar_events.id", source="trips")`.

### `ai_agents/tools/__init__.py`

Re-exports all five tool classes and their result dataclasses. Enforces the
layering law in the module docstring.

### Tests (`ai_agents/tests/test_tool_*.py`)

| File | Marker | What it proves |
|---|---|---|
| `test_tool_inventory.py` | none (hermetic) | Known SFO→JFK options incl. UA201B; NYC hotels incl. Luxury Loft; option_id populated; impossible intent → empty; bad segment_type → `ToolError`. |
| `test_tool_policy_eval.py` | `db` | UA201 economy → IN; UA201B business 5.5h → OUT+AIR-CABIN+VP; Luxury Loft $400 NYC → OUT+HOTEL-CAP; Marriott $239 → IN; **no verdict ever carries SUSTAIN**; other-org → 0 rules. |
| `test_tool_policy_retrieval.py` | `db` | "hotel nightly rate cap" → HOTEL-CAP in top-5; SUSTAIN chunk IS retrievable; similarities [0,1] desc; injected embedder is called (mock asserted); mismatched embedder degrades gracefully; other-org → empty. |
| `test_tool_profile.py` | `db` | Employee email/role/manager_id; **preferences == {} (never None)**; recent_trips includes SFO→JFK; `user_id=None` → ToolError; cross-user disjoint trip IDs; cross-org → ToolError. |
| `test_tool_itinerary_calendar.py` | `db` | 2 segments (UA201 flight + Marriott hotel); 2 Summit calendar events, location "New York, NY"; details round-trip; `user_id=None` → ToolError; **cross-user segment IDs disjoint**; cross-org → empty tuples. |

`test_no_backend_import.py` updated with all six new `ai_agents.tools.*`
imports — layering law is machine-enforced.

---

## Scoping Summary

| Tool | Org scope | User scope |
|---|---|---|
| Inventory | none (global mock) | none |
| PolicyEval | `policies.organization_id` via JOIN + `status=active` | none |
| PolicyRetrieval | `policies.organization_id` via JOIN | none |
| Profile | `User.organization_id` | `User.id`, `Trip.traveler_id` |
| ItineraryCalendar | `Trip/CalendarEvent.organization_id` | `Trip.traveler_id`, `CalendarEvent.user_id`; segments via scoped trip ids |

---

## Key Decisions

**1. Org-scoping for policy tools via explicit JOIN to `policies`.**  
`policy_rules` and `policy_embeddings` have no `organization_id` column —
they inherit tenant context from their parent `Policy` via `policy_id FK`.
A naive query against either table without the JOIN would silently return
cross-tenant rows. Both `PolicyEvalTool` and `PolicyRetrievalTool` JOIN to
`policies` and filter on `policies.organization_id`. This is the single most
critical correctness point in P4-T4.

**2. SUSTAIN clause produces no verdict — structural guarantee.**  
`SUSTAIN` exists only in `policy_content.py`'s `CLAUSES` list, not in `RULES`.
No `policy_rules` row has `source_clause_ref="SUSTAIN"`. The rule-selection
query returns 7 rows at most; none is SUSTAIN. No special-case code needed.

**3. Retrieval embedder is injected.**  
`PolicyRetrievalTool.__init__(embedder=None)` defaults to
`get_embeddings_provider()` (→ `LocalEmbeddingsProvider` in dev). This is the
**same embedder that seeded `policy_embeddings`** — if a mismatched embedder
is injected, cosine similarity degrades gracefully but results are meaningless.
Proved by the mock-embedder test and the mismatched-embedder degradation test.

**4. No threshold / no answer assembly.**  
`PolicyRetrievalTool` returns raw top-k chunks and similarities only.
The "policy is silent" decision and confidence threshold are deferred to P4-T6
(grounded retrieval flow). `validate_grounding` is the calling agent's
responsibility, not the tool's.

**5. No registry.**  
Tool routing and the registry are P4-T7. Tools are constructed and called
directly by tests; a `GroundingTool` Protocol provides type-checker coverage
without enforcement.

---

## Validation Evidence

- **15 hermetic tests** pass: 14 in `test_tool_inventory.py` + 1 in
  `test_no_backend_import.py` (proves `ai_agents.tools.*` imports zero
  `backend.*` modules).
- **40 live-DB tests** self-skip when Postgres is unreachable — correct
  CI-safe behaviour. On live compose (pgvector/pg16 + seeded schema), all
  40 pass against the P3-T5 seed.
- **Cross-user scoping**: both same-org (employee vs manager) and cross-org
  (random UUID org) isolation tested for Profile and ItineraryCalendar.
- **Tools are read-only**: `grep -rn "session.add|session.commit|session.delete"
  ai_agents/tools/` → 0 matches.
- **Only retrieval touches a provider**: `LlmProvider` is absent from all five
  tool files; `EmbeddingsProvider` appears only in `policy_retrieval.py`.
- **No migration**: `alembic check` / autogenerate drift → empty (no new
  table/column/index added).
- **`/health` 200**: app boots clean with the tools package present.
- **ruff clean**: `ruff check ai_agents/tools/ ai_agents/tests/test_tool_*.py` →
  "All checks passed!".
- **mypy clean**: `mypy ai_agents/tools/ --ignore-missing-imports` → "Success:
  no issues found in 7 source files".

---

## Deviations

None.

---

## Issues Found and Fixed in QA

1. **`policy_rules` / `policy_embeddings` have no `organization_id`.**  
   Org-scoping requires a JOIN to `policies` (discovered during exploration,
   implemented correctly from the start; documented explicitly as the critical
   correctness point).

2. **`MockInventoryProvider` exposes `search_flights` / `search_hotels`, not a
   generic `search(segment_type=)`.**  
   `InventoryTool` dispatches by `segment_type` to the two typed methods.

3. **SQLAlchemy `Mapped[object]` for `Date` / `Numeric` / `TIMESTAMP` columns.**  
   mypy cannot infer the concrete types; `# type: ignore[arg-type]` annotations
   added on the model-attribute accesses in `profile.py` and
   `itinerary_calendar.py`.

4. **6 ruff items** (4 auto-fixed by `--fix`, 2 manual line-wraps): F401 unused
   imports in test files, I001 unsorted import block in `policy_eval.py`, E501
   long lines in `policy_eval.py`, `profile.py`, and `itinerary_calendar.py`.

---

## Known Gap (carry forward)

Confirm the CI `mypy` / `ruff` target covers `ai_agents/` and `services/`.
If the CI config still reads `mypy backend config`, this sub-phase's mypy pass
is local-only; extending the target is a small separate task.

---

## Next

- **P4-T5** — real-provider embeddings + idempotent re-index of `policy_embeddings`.
- **P4-T6** — grounded retrieval flow: threshold, "policy is silent" decision,
  answer assembly.
- **P4-T7** — tool registry / routing (the `GroundingTool` Protocol seam is
  already in place).
