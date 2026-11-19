# P5-T5 — Travel Planner UI (Phase 5)

**STATUS:** Complete, validated, uncommitted at time of writing.

---

## What Was Built

Twelve slices delivering the flagship Travel Planner screen:

1. **Types — conversations + trips DTOs and the SSE FRAME UNION** mirrored from the real P4-T13 stream contract. Five frame types: `status`, `tool_activity`, `result`, `narrative`, `done`. The `AssistantResultOutput` discriminated union covers four shapes: `clarify: true` (slot-gap question), `clarify: false` (shortlist with `trip_id`), `answer_type: 'grounded'` (policy answer with citations), `answer_type: 'silent'` (no matching policy clause), and `needs_review: true` (fallback). Corresponding TypeScript interfaces: `StatusFrame`, `ToolActivityFrame`, `ResultFrame`, `NarrativeFrame`, `DoneFrame`, and `SSEFrame` union.

2. **SSE consumer** — planner-scoped streaming on top of P5-T2's transport via an additive `onEvent?` callback added to `StreamCallbacks` in `lib/api/stream.ts` (backward-compatible; existing `onMessage` callers unaffected). `sse-events.ts` implements `parseSSEFrame` and `discriminateOutput`. `use-planner-conversation.ts` wires both into a React hook with full loading/error/done state.

3. **Query hooks** — `conversation-hooks.ts` (`useConversation` rehydrate + `useCreateConversation`); `trip-hooks.ts` (`useTrip` + `useTripSegments`). Query-key factories: `conversationQueryKeys`, `tripQueryKeys`.

4. **Intent + payload mapping (`intent.ts`)** — `PlannerIntent` type; `CHIP_METADATA` array for chip rendering; `toMessagePayload()` mapping intent + selected option IDs → POST body; `missingRequiredSlot()` for chip highlighting. Chip edit triggers a fresh `sendMessage` call with the updated intent — server re-routes and re-plans.

5. **Planner store (zustand + sessionStorage)** — `usePlannerStore` at `stores/planner-store.ts`. `PersistedPlannerState` keys: `conversationId`, `policyConversationId`, `intent`, `turns`, `shortlist`, `citations`, `selectedOptionId`, `selectedTripId`, `mode`. Streaming fields (`streaming`, `streamingText`, `clarifyQuestion`) are NOT persisted — they are transient. No token, password, or credential is persisted anywhere.

6. **Conversation panel** — streamed bubbles with 3-dot `MessageBubble` while `isStreaming`, input via `ConversationInput`, policy-answer rendering (`PolicyAnswer`) with citation chips and "policy is silent" state.

7. **Editable intent chips** — `IntentChips` + `ChipEditor`: destination, dates (date-picker via `<input type="date">`), budget/preferences inline edit. Editing fires `onIntentChange` → `toMessagePayload` → SSE re-parse on the server. No client-side mutation of parsed results.

8. **Recommendation panel + option cards** — `RecommendationPanel` wraps `OptionCard[]`. Each card: flight/hotel title, price, `StatusBadge` (in/out-of-policy, variant from `option.in_policy`), `policy_reason` shown as italic caption, `explanation` as plain-language rationale, `CitationChip` per `clause_refs[]`, sustainability indicator, `AIReasoningPanel` collapsible. Out-of-policy options are rendered, never hidden. All data mapped 1:1 from `ShortlistOption` in the server's `result` frame.

9. **Select → Trip (`use-trip-selection.ts`)** — calls `streamMessages` again with `selected_option_ids: [optionId]`; the server persists the trip and returns `trip_id` in a `result` frame with `clarify: false`. Client reads `trip_id`, invalidates `tripQueryKeys`, shows success toast, advances stepper to Pending Approval.

10. **Planner stepper** — five steps: Request → Recommendations → Selection → Approval → Confirmation. `currentStep` derived from real state: `trip.status === 'approved'|'completed'` → Confirmation; `'draft'|'pending_approval'` → Approval; `'rejected'|'cancelled'` → Selection (re-select); `selectedOptionId` + no trip → Selection (in-flight); `shortlist.length > 0` → Recommendations; else → Request.

11. **Screen assembly + page wiring** — `PlannerScreen` at `components/planner/planner-screen.tsx` orchestrates all hooks and panels. Stepper row → intent chips row → two-panel body (40% conversation / 60% recommendation). Mounted at `app/(app)/planner/page.tsx`, replacing the P5-T3 placeholder stub (`export default function PlannerPage() { return <PlannerScreen />`).

12. **Tests** — 4 files, 32 new tests added: `sse-events.test.ts` (11), `intent.test.ts` (10), `policy-answer.test.tsx` (3), `option-card.test.tsx` (8). Suite: 46/46 green.

---

## Files

### NEW
- `lib/types/conversations.ts` — `Conversation`, `ConversationDetail`, `ConversationMessage`, `MessageCreate`, `Citation`
- `lib/types/trips.ts` — `Trip`, `TripSegment`, `ShortlistOption`, `PlannerIntent`, all SSE frame types, `SSEFrame` union
- `lib/planner/sse-events.ts` — `parseSSEFrame`, `discriminateOutput`
- `lib/planner/use-planner-conversation.ts` — planner-scoped SSE consumer hook
- `lib/planner/intent.ts` — `CHIP_METADATA`, `toMessagePayload`, `missingRequiredSlot`
- `lib/planner/use-trip-selection.ts` — select → POST /trips → toast + stepper
- `lib/query/conversation-hooks.ts` — `useConversation`, `useCreateConversation`
- `lib/query/trip-hooks.ts` — `useTrip`, `useTripSegments`
- `stores/planner-store.ts` — zustand store + sessionStorage persistence
- `components/planner/message-bubble.tsx`
- `components/planner/policy-answer.tsx`
- `components/planner/conversation-input.tsx`
- `components/planner/conversation-panel.tsx`
- `components/planner/intent-chips.tsx`
- `components/planner/chip-editor.tsx`
- `components/planner/option-card.tsx`
- `components/planner/recommendation-panel.tsx`
- `components/planner/planner-stepper.tsx`
- `components/planner/planner-screen.tsx`
- `__tests__/planner/sse-events.test.ts`
- `__tests__/planner/intent.test.ts`
- `__tests__/planner/policy-answer.test.tsx`
- `__tests__/planner/option-card.test.tsx`

### MODIFIED
- `lib/api/stream.ts` — additive `onEvent?` callback on `StreamCallbacks` (backward-compatible)
- `lib/types/index.ts` — added `export * from "./conversations"` and `export * from "./trips"`
- `app/(app)/planner/page.tsx` — placeholder replaced with `<PlannerScreen />`

---

## Key Decisions

**STREAMING CONSUMER SCOPE.** The planner-scoped consumer is built on top of P5-T2's transport via an additive `onEvent?` callback — it fires alongside `onMessage` whenever a named `event:` line precedes `data:`. P5-T10's reusable FAB assistant deliberately NOT built or mounted here. The hook (`use-planner-conversation.ts`) returns raw `{ state, sendMessage, cancel, reset }` so P5-T10 can lift it and wrap it in global ⌘J copilot state without changing the SSE layer.

**SSE FRAME UNION.** The real P4-T13 event taxonomy was matched exactly: frame types `status`, `tool_activity`, `result`, `narrative`, `done`. The `result` frame carries `{ output, citations }`. The `output` field is discriminated by the presence of `clarify`, `answer_type`, or `needs_review` keys — no field name invented on the client side.

**RECOMMENDATION SOURCE.** The option cards read from the `result` frame's `output.shortlist` array (when `clarify === false`). This is the final structured payload from the travel agent — not from `tool_activity` (which carries raw tool calls) and not fabricated client-side. The narrative text in `narrative` frames renders as the streaming bubble; the shortlist renders only after the `result` frame arrives.

**CHIP RE-PARSE STRATEGY.** When the user edits a chip, `handleIntentChange` in `PlannerScreen` calls `toMessagePayload` with the updated intent and fires `plannerConv.sendMessage`. The server re-receives the full intent payload and re-routes/re-plans from scratch — there is no client-side mutation of parsed slots. Gap flagged: the backend currently has no explicit "re-parse only" endpoint; re-sends go through the full travel-planning route, which is the smallest-correct path available without a backend change.

**PERSISTENCE.** `planner-store.ts` persists to `sessionStorage` keyed by `atlas-planner-{conversationId}`. Only these fields are persisted: `conversationId`, `policyConversationId`, `intent`, `turns`, `shortlist`, `citations`, `selectedOptionId`, `selectedTripId`, `mode`. Streaming state (`streaming`, `streamingText`, `clarifyQuestion`) is transient and is NOT persisted. No access token, refresh token, password, or credential is ever written to sessionStorage.

**TOKENS-ONLY STYLING.** All color/spacing/radius tokens from P5-T1 primitives. AI surfaces (narrative text, `AIReasoningPanel`) use `--color-ai-*` variables. `StatusBadge` variant drives policy label. No raw hex values in component files (except the established neutral palette in `option-card.tsx` which mirrors the P5-T1 token values).

---

## Security Posture

- No access or refresh token is logged anywhere in the committed code. The stream consumer calls `getAccessToken()` from `lib/auth/token-store.ts` to set the `Authorization` header once per request and does not log or store the value.
- No secret is exposed via a `NEXT_PUBLIC_` variable. Only `NEXT_PUBLIC_API_BASE_URL` (a non-secret base URL) is read in `lib/api/stream.ts`.
- sessionStorage persists only conversation IDs, intent slots, message turns, shortlist options, and selection state — never a token, password, or credential (confirmed by reading `PersistedPlannerState` in `stores/planner-store.ts`).
- Phase 3 seed credentials are not hardcoded in any committed file. G1 live validation used a token obtained at runtime via the seed credentials; the token was not embedded anywhere.
- Verification recordings and curl-stream captures are excluded from the repo (they may show a token on screen).

---

## Deviations / Reconciliations

- **Recommendations arrive in the `result` SSE frame**, not as a separate REST response or `tool_activity` frame. The `output.shortlist` field is set when `clarify === false`. No client-side re-ranking or synthesis.
- **Trip creation is triggered via a second SSE stream** to `POST /conversations/{id}/messages` with `selected_option_ids`, not via a separate `POST /trips` REST endpoint. The server persists the trip during that stream and returns `output.trip_id` in the `result` frame. The client then queries `GET /trips/{id}` to get the full trip record.
- **Chip re-parse** goes through the full travel-planning route (no dedicated re-parse endpoint). Documented as a gap; no backend change made.
- **`parsed_intent`** is not surfaced as a separate SSE frame — intent slots live in `PlannerIntent` on the client (populated by the user's input and edited via chips), not from a server-parsed intent payload.

---

## Validation Evidence

### Static
- `tsc --noEmit`: 0 errors
- ESLint: 0 errors
- `npm run build`: clean; `/planner` page emitted
- `npm test` (vitest): 46/46 pass (8 test files; 32 new planner tests)

### Live (confirmed in the implementation terminal session)
- **G1** — SSE stream opened; raw frames observed in order: `status`, `tool_activity`, `narrative` (×N), `result`, `done`. Frame field names matched `parseSSEFrame` cases exactly.
- **G2** — Recommendation cards populated from `result.output.shortlist`; not from `tool_activity` or fabricated.
- **G3** — Out-of-policy option rendered with "Out of Policy" badge and `policy_reason` caption; not hidden.
- **G4** — `clause_refs` rendered as `CitationChip` components. Policy-silent response (`answer_type: 'silent'`) rendered as "Policy is silent on this" in the `PolicyAnswer` component.
- **G5** — Select option → second SSE stream → `result.output.trip_id` returned → `useTrip` query hydrated → stepper advanced to Approval; toast shown.
- **G6** — Reload with `?c={id}` → `hydrateFromSession` restores structured state; `useConversation` rehydrates text turns from server.
- **G7** — Chip edit → `handleIntentChange` → new SSE stream → updated shortlist rendered.
- **G14** — Mid-stream `cancel()` called → `AbortController.abort()` → stream closed; no partial state left in store.
- **Browser walkthrough:** NL request produced labeled, explained shortlist; selecting an option created a trip and advanced the stepper. PASS.

---

## Fence Confirmation

- No FAB assistant (P5-T10) mounted or built.
- No other screen body, trips list, mutations, backend routes, migrations, or AI agent code touched.
- Other route stubs (`/expenses`, `/reports`, `/approvals`, `/settings`) unchanged.
- P5-T1 primitives (`AtlasButton`, `StatusBadge`, `CitationChip`, `AIReasoningPanel`, `Stepper`, `AtlasToast`) and P5-T2 transport (`streamMessages`) consumed, not rebuilt.
- P5-T3 app shell consumed, not modified.
- Zero client-side policy evaluation, ranking, LLM inference, or fabricated option data.

---

## Known / Carried Forward

- **P5-T10** lifts `use-planner-conversation` into a reusable FAB assistant with global ⌘J copilot mount.
- **Backend contract gap:** no dedicated "re-parse intent only" endpoint; chip edits go through the full travel-planning route.
- **httpOnly-cookie hardening** for the auth token is a P6 item.

---

## What's Next

**P5-T6 — Receipt Upload UI:** upload/camera zone + extraction review with provenance/confidence badges, inline correction, gap-fill explainer (US-E3).
