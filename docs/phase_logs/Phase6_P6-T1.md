# Phase 6 · P6-T1 — Frontend ↔ Backend Integration

## Goal

Prove every screen sources from live APIs with real loading/empty/error states and a working
auth-refresh seam; remove all removable mock data from production screens.

## What was done

- Ran the first full FE↔BE integration pass against the live stack; confirmed live data source,
  error envelope surfacing, loading/empty states, and the auth-refresh seam (401 → silent refresh
  → retry; refresh-fail → /login) for every screen.
- Found and fixed two latent, app-breaking seam bugs that made the app appear finished while
  stopping it from actually working against real APIs:
    1. **Login session bug** — `useLogin.onSuccess` stored tokens and cached `/me` but never called
       `useAuthStore.getState().setSession()`. `AuthGuard` saw status `"unauthenticated"` and
       bounced every user back to `/login` immediately after every successful login.
    2. **Zustand-v5 render loop** — `useSession()` returned a new object literal every render.
       Zustand v5's `useSyncExternalStore` treated the changed snapshot as a state change, causing
       "Maximum update depth exceeded" on every authenticated screen. Fixed with `useShallow`.
- Reconciled DTO mirrors to real envelopes (frontend-side only, backend response/schema untouched):
    - `TripSegment.amount_usd: number` → `amount: string | null` (Decimal→JSON string)
    - `segment_type` enum widened to `"flight" | "hotel" | "rail" | "car"`
    - `ConversationMessage.role` widened to `"user" | "assistant" | "system" | "tool"`
- Closed three addendum gates:
    - **A3**: No client-side intelligence or client-set provenance confirmed — zero summation,
      matching, policy-verdict, or ranking logic in product frontend; `field_provenance` is
      display-read only; `useCorrectReceiptField` sends `{field, value}` only (server flips provenance).
    - **B3**: State-changing actions confirmed honest — submit/approve/reject success and induced
      failure tested live; live provenance flip `ai_inferred→user_edited` confirmed via PATCH
      (server-authoritative, client sent no provenance field).
    - **B4**: Trust-cue chips confirmed bound to live response fields with exact `DATABASE_SCHEMA`
      enum strings (`deterministic|ai_inferred|user_edited`, `pending|auto_approved|approved|
      rejected|changes_requested`, `receipt|card_txn|manual|trip`, etc.).
- Stub cosmetic pass (Option B): fixed copy-paste bug on `/audit` (heading was "Expenses");
  neutralized "Coming in Phase 5" copy on `/trips` and `/users`. Routes remain unwired placeholders.
- Added Playwright e2e harness to capture proofs: auth-refresh (B1), per-role screen sweep (B2/B5),
  state-changing honesty (B3). 10/10 tests passing; 25 screenshots captured.
- Annotated the dev-only component gallery with a production guard (`notFound()` in production)
  and a `// DEV-ONLY GALLERY` comment.

## Key decisions

- **Frontend-mirror-side reconciliation only.** The backend response and schema were never
  changed; all DTO drift was corrected by aligning the client type to what the live API actually
  returns. Drift guard (backend/, ai_agents/, database/, config/) confirmed empty.
- **Mock vs. gap distinction.** There were no removable production mocks (all hardcoded data was
  in `__tests__/` or the dev-only gallery). "Gap" affordances (NFR-SEC4 image unavailable,
  `/audit` endpoint absent) were kept honest and documented, not faked.
- **Stack-limited proofs handled by attestation.** B3.1 (select-trip UI) and the full receipt-
  upload path could not be live-exercised because the local LLM stub returns `clarify` (no
  selectable shortlist) and `POST /receipts` 500s on the `boto3` gap. Both were handled by
  live API confirmation + code-read attestation and flagged, not green-washed.

## Verification

- `tsc` clean · ESLint clean · `next build` 17/17 routes
- Playwright 10/10 (6 main + 4 B3 state-actions) · 25 screenshots
- Drift guard empty (backend/, ai_agents/, database/, config/)
- A3 grep clean: zero client summation, matching, policy verdict, ranking, or provenance writes
- B4 enums confirmed exact against `DATABASE_SCHEMA`

## Carried gaps (backend / out of P6-T1 scope → P6-T2+)

- `boto3` missing from prod image → `POST /receipts` always 500 in this stack
- `GET /audit` endpoint genuinely absent from backend source
- Planner LLM is a local stub; returns `clarify`, not a selectable shortlist
- Loosely-typed backend schemas (`dict[str,Any]` / bare `str` enums) for `risk_summary`,
  receipt JSONB, analytics spend — frontend mirrors the concrete shape but backend provides no
  Pydantic validation
- NFR-SEC4: receipt image serving absent; honest "image unavailable" affordance kept
- Assistant detail-route context (`/trips/[id]`, `/expenses/[id]`) stubbed to `general` → P6-T2
