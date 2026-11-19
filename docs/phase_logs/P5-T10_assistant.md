# P5-T10 — AI Assistant Slide-Out (Reusable Chat Surface)

**Sub-phase:** P5-T10  
**Title:** Reusable AI Assistant Surface — US-E2  
**Date:** 2026-06-16  
**Author:** Garv Singh Saini  
**Status:** COMPLETE — closes Phase 5

---

## WHAT WAS BUILT

### NEW

- **`frontend/lib/assistant/tool-activity.ts`**  
  Pure mapper: persisted `tools_used` trace → per-tool `ToolStep[]` (Inventory,
  Policy Evaluation, Policy Retrieval, User Profile, Itinerary), `status`
  complete/failed; `summarizeToolActivity`. Never invents steps not in the trace.

- **`frontend/lib/assistant/context.ts`**  
  `deriveContextFromPath`: `/trips/{id}` → `travel_planning` + refId;
  `/expenses/{id}` → `expense` + refId; everything else → `general`.

- **`frontend/stores/assistant-store.ts`**  
  Global Zustand store: `turns`, context (`contextType`/`contextRefId`/
  `contextLabel`), open-state, `openScoped`/`openWithContext` (re-scope resets
  the conversation), sessionStorage persist/hydrate
  (`atlas-assistant-{id}` + `atlas-assistant-active` pointer key).

- **`frontend/lib/assistant/use-assistant-conversation.ts`**  
  Streaming consumer on `stream.ts`. Lazy context-scoped conversation create;
  frame→turn mapping (status, tool_activity, narrative, result, done); abort/
  retry/teardown; GET backfill via `backfillFromServer` + `loadConversation`
  rehydrate.

- **`frontend/components/atlas/assistant/assistant-panel.tsx`**  
  Conversation body filling the drawer slot. Scrollable turn list, empty/
  streaming/loading states, context chip ("About: …"), sticky composer.
  sessionStorage-first hydration; GET fallback only when session snapshot absent.

- **`frontend/components/atlas/assistant/assistant-turn.tsx`**  
  Single turn renderer composing P5-T1 primitives and reused P5-T5 pieces:
  `MessageBubble` (plain content), `PolicyAnswer` (grounded/silent), role-bounded
  refusal block (`ShieldAlert`, NFR-SEC6), `ToolActivityIndicator`,
  `ConfidenceIndicator` + `AIReasoningPanel` (degraded — real data only).

- **`frontend/components/atlas/assistant/assistant-input.tsx`**  
  Sticky textarea composer. Enter sends; Shift+Enter inserts newline. Auto-grows
  to 120 px max. Disabled while streaming.

- **`frontend/components/atlas/assistant/tool-activity-indicator.tsx`**  
  Active (Loader2 + blue) / Complete (Check + green) / Failed (X + red) per
  step. Auto-expands while streaming, auto-collapses to summary after response.
  "Working…" generic state during streaming.

- **`frontend/__tests__/assistant/`** — 5 test files / 29 tests:  
  `tool-activity.test.ts` (8), `context.test.ts` (4), `assistant-store.test.ts`
  (5), `assistant-turn.test.tsx` (7), `use-assistant-conversation.test.tsx` (5).
  Mocks at module boundary (`@/lib/api/stream`, `@/lib/api/client`).

### MODIFIED (no planner, no backend)

- **`frontend/lib/types/conversations.ts`** — additive widen of
  `ConversationContextType` to all 4 backend enum values:
  `travel_planning | expense | policy_qa | general`. Non-breaking for the
  planner which uses a subset.

- **`frontend/components/atlas/assistant-drawer.tsx`** — placeholder body
  replaced with `<AssistantPanel>`; desktop overlay (`bg-black/40`, ~60% dim)
  + click-outside added (mobile backdrop already present).

- **`frontend/lib/hooks/use-app-shell.ts`** — assistant open-state now backed
  by the global store; route-context derived from `usePathname` on open
  (FAB/launcher/⌘J wiring untouched).

- **`frontend/components/atlas/index.ts`** — barrel exports for all 4 assistant
  components.

---

## KEY DECISIONS

- **Reuse = Option 2 (build-parallel):** planner zero-diff; reused
  `parseSSEFrame`/`discriminateOutput`, `PolicyAnswer`, `MessageBubble`, and
  P5-T1 primitives by read-only import. No extraction refactor → blast radius 0.
- **`explain_with_llm: true`** sent on every stream call so chat receives a
  streamed narrative alongside the structured result.
- **Re-scoping to a new subject starts a fresh conversation;** route-context is
  applied only on the FAB/hotkey open path so an explicit `openScoped` is never
  overwritten.
- **Honest degrades over fabrication throughout** — confidence shown only for
  `route='llm'`; reasoning limited to the route signal; follow-ups omitted
  (not in any DTO); confidence omitted on GET-only rehydration.

---

## RECONCILE FINDINGS (live-verified; code wins over docs)

- **SSE taxonomy:** `status → result → done` (`tool_activity`/`narrative`
  conditional). Shapes match the parser exactly.
- **Tool activity:** no agent populates `output.tool_calls` today → the live
  `tool_activity` SSE frame is never emitted. The affordance sources from the
  persisted trace via GET (a defensive live handler is retained for future-
  proofing).
- **Confidence/route:** present only in the `status` frame (not in GET).
  Rendered for `route='llm'`; `route='deterministic'` shows a "Deterministic"
  badge; omitted on rehydration. No fabricated %.
- **Reasoning:** `ai_decision_logs` not exposed → the accordion degrades to
  the real route signal only.
- **Follow-ups:** not in any DTO → omitted entirely (no client invention).
- **Context mapping:** `POST /conversations` accepts + echoes `context_type` +
  `context_ref_id` for `travel_planning`/`expense` (live-verified).

---

## ISSUES FOUND & FIXED / NEW FINDING (flagged for P6)

**Persistence finding:** `policy_qa`/`general` routes do **not** persist an
assistant message — `GET /conversations/{id}` returns only user turns for those
routes; `done.message_id` is the user message id there. → **sessionStorage is
the primary reload-survival path; GET is a best-effort reconcile only.**

Two bugs discovered and fixed during live API testing:

1. **Backfill clobber bug** (`backfillFromServer`): the original message lookup
   `detail.messages.find(m => m.id === messageId)` would match the user message
   for policy_qa/general (same id) and try to read `tool_calls` from it (always
   null). Fixed: added `m.role === 'assistant'` filter.

2. **Hydration clobber bug** (`assistant-panel.tsx` mount): `loadConversation`
   was called unconditionally on mount, overwriting richer sessionStorage turns
   with a GET that returns user-only history. Fixed: `loadConversation` is now
   called only when sessionStorage hydration returned no turns.

---

## VALIDATION EVIDENCE

**Live-run (against seeded backend):**
- G1: `POST /conversations` (general) → create ✓; stream `policy_qa` question →
  `status → result(silent) → done`; frames match parser ✓; GET backfill ✓.
- G2: context-scoped create with `context_type: travel_planning` +
  `context_ref_id` and `context_type: expense` + `context_ref_id` → both
  echo correct DTOs ✓.
- G3: "policy is silent" rendered faithfully (seeded RAG returns
  below-threshold → `answer_type: silent` — a seed-data limitation, flagged).

**Static / grep-verified:**
- G4/G7/G8/G10/G11: grep-clean — no new transport (uses `streamMessages`); no
  token logging; no `NEXT_PUBLIC_*` secret; no client answer/suggestion
  generation.
- G9: planner unbroken — zero planner-file edits; full suite **178/178 pass**.
- G13: `tsc --noEmit` 0 errors · `next lint` clean · `next build` ✓ (17 routes)
  · 29 new assistant tests pass (streaming incl. malformed/abort/error,
  context-scoped create, citation/silent render, tool mapper, persistence).
- G14: mid-stream abort fails closed with retry; dismiss-mid-stream cancels —
  both unit-tested.

**Not live-run (recorded honestly):**
- G5: role-bounded refusal (code-verified in `backend/app/modules/ai/guardrails/
  actions.py` → `needs_review`; render unit-tested).
- G6: GET-rehydration path (covered by sessionStorage per the persistence
  finding; live GET test omitted because policy_qa/general don't produce a
  server-side assistant message to rehydrate from).

---

## PHASE 5 CLOSEOUT

**DoD routes (all compile; trust cues present):** dashboard, planner, receipts,
expenses, approvals, analytics, policy, audit, users, trips, login + the global
assistant — all present with trust cues (citations, confidence, provenance)
throughout.

**End-to-end checkpoint:** The terminal contract walk (curl → stream → GET) is
done. The full browser walkthrough (FAB / ⌘J open → stream → navigate → reload →
scoped open → refusal → dismiss) is the manual script — the terminal walk can't render
the slide-out. The rigorous frontend↔backend integration pass is P6-T1.

---

## DEFERRED / FLAGGED (for P6)

| Gap | Impact | Owner |
|-----|--------|-------|
| Backend doesn't persist assistant messages for `policy_qa`/`general` | GET can't fully rehydrate; sessionStorage covers it in-session | P6 |
| `confidence`/`reasoning` not exposed by GET (`ai_decision_logs` internal) | Omitted on reload; shown only for live turns | P6 |
| No per-tool SSE stream events | Tool-activity affordance sourced from persisted trace only | P6 |
| No suggested follow-ups in the contract | Feature omitted (no client invention) | P6 |
| Seeded policy RAG returns below-threshold | Live answers are "silent"; grounded-citation demo needs seed embeddings | Seed / P6 |
| `/trips/[id]`, `/expenses/[id]` detail routes stubbed | Route-auto-scoping is latent; imperative `openScoped` works today | P6 |
