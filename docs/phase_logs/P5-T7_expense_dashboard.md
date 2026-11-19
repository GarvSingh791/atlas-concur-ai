# P5-T7 — Expense Dashboard UI

| Field        | Value                                       |
|-------------|---------------------------------------------|
| Sub-phase   | P5-T7                                       |
| Title       | Expense Dashboard UI — report list + detail |
| Date        | 2026-06-16                                  |
| Author      | Garv Singh Saini                            |

Stories: **US-E4** (assembled report → review → submit; pre-submit flags) and **US-E5**
(status tracking; rejection reason + what to fix).

---

## WHAT WAS BUILT

Replaced the `/(app)/expenses` P5-T3 placeholder stub with the full employee Expense
Dashboard (~1,617 lines of new code). Composes P5-T1 primitives only. Split:
types → lib → hooks → components → page.

### Types

- `lib/types/expenses.ts` (NEW) — DTO mirrors reconciled to the real backend
  (`backend/app/modules/expense/schemas.py`, `database/models/expense_report.py`,
  `database/models/expense.py`): enums `ExpenseReportStatus`, `ExpenseCategory`,
  `ExpenseEntryStatus`, `ExpenseMatchSource`; `ReportListItem`, `ExpenseReport`
  (with `pre_submit_flags: unknown`), `ExpenseEntry`, `ReportTransitionRequest`,
  `ReportListParams`. Money fields typed as `string` to mirror the wire shape
  (Pydantic v2 serializes Decimal → JSON string, e.g. `"1351.00"`).
- `lib/types/index.ts` (MODIFIED) — barrel re-exports `./expenses`.

### Domain Logic (`lib/expenses/`)

- `format.ts` (NEW) — `formatCurrency` (coerces string|number), `formatDate`,
  `CATEGORY_LABELS`, `CATEGORY_ICONS`, `REPORT_STATUS_LABELS`, `matchPresentation`.
  No client math of any kind.
- `readiness.ts` (NEW) — `normalizePreSubmitFlags(raw): ReadinessSummary`.
  Handles `null`, agent shape (`{flags:[{severity:"blocking"|"advisory", reason,
  source_clause_ref, entry_ref, flag_type}]}`), and seed shape
  (`{warnings:[{severity:"high"|"medium"|"low", message, clause_ref}]}`).
  Maps `blocking/high/critical → "issue"`, everything else → `"review"`.
  Computes `level: "ready"|"review"|"issue"`. `suggestedFix(flag)` returns hint
  text by flagType (falls back by severity for the seed shape). Unknown severities
  default to `"review"` (never crash).
- `submit-gate.ts` (NEW) — `computeSubmitGate(level): SubmitGate`.
  `issue` → red/disabled; `review` → amber/requiresConfirm; `ready` → green/enabled.
  Presentation only — no policy re-evaluation, no total recompute.

### Hooks (`lib/query/expense-hooks.ts`, NEW)

- `useExpenseReports(params?)` → `GET /expense-reports`
- `useExpenseReport(id)` → `GET /expense-reports/{id}`
- `useReportEntries(id)` → `GET /expense-reports/{id}/entries`
- `useSubmitReport()` mutation → `PATCH /expense-reports/{id}` with
  `If-Match: <version>` header. Stale/illegal → 409 surfaced via `ApiError` envelope.

### Components (`components/expenses/`, all NEW)

- `report-status-badge.tsx` — `ReportStatusBadge({status})` on the generic `Badge`
  primitive. Five lifecycle states: `draft/submitted/approved/rejected/reimbursed`.
  Token classes only (no raw hex).
- `report-list-toolbar.tsx` — Status filter (`DropdownMenu`) + title search (`Input`)
  + disabled Assemble button with a visible flagged note. Note is visible text (not
  a tooltip) to avoid the primitive's disabled-tooltip `<button>` nesting issue.
- `report-list.tsx` — `DataTable<ReportListItem>`: Report (title + "Needs your review"
  emphasis for rejected/issue reports), Amount, Status (`ReportStatusBadge`), Policy,
  Submitted, Actions. Client-side title search; sort mapped to allow-listed backend
  fields; pagination.
- `entries-table.tsx` — `DataTable<ExpenseEntry>` with expand/collapse.
  Columns: Category (icon+label), Description/Vendor, Amount, Match (`match_source`
  label: auto vs manual vs unmatched), Policy (`StatusBadge` in-policy/out-of-policy
  from entry `status === "flagged"`).
- `entry-lineage.tsx` — Per-entry lineage expansion. Shows `match_source` label,
  entry metadata, trip segment ref. Receipt match shows metadata note + explicit
  "opaque object-store reference" text (no image, NFR-SEC4). Null `match_source` →
  "Manually entered — no automatic source linked".
- `readiness-panel.tsx` — Renders normalized `ReadinessSummary` as a `Card`.
  Ready → "no issues" message. Review/issue → flag list with message, suggested fix,
  `CitationChip` for clauseRef.
- `submit-bar.tsx` — Renders only for `status === "draft"`. Green: `AtlasButton`
  enabled, direct submit. Amber: opens `Modal` variant="confirm" first. Red:
  `AtlasButton` disabled with `disabledReason` tooltip. On 409 CONFLICT: specific
  "This report changed" error toast. On success: "Report submitted" success toast.
- `rejection-notice.tsx` — Renders for `status === "rejected"`. Shows
  `pre_submit_flags` as "Issues to resolve" with `CitationChip`. "No reason recorded"
  when no flags. Always includes honest gap note (reviewer's comment not exposed by
  the employee API).
- `report-detail.tsx` — Orchestrator: back button, header (title / total /
  `ReportStatusBadge`), `RejectionNotice`, `ReadinessPanel`, entries table, `SubmitBar`.
  Loading: `LoadingState` skeletons. Error: `ErrorState` with retry.
- `expenses-screen.tsx` — Single-screen orchestrator. `selectedReportId` state;
  list view ↔ detail view. Header shown only in list view.

### Page

- `app/(app)/expenses/page.tsx` (MODIFIED) — Was: `EmptyState` placeholder ("Travel
  Planner"). Now: `return <ExpensesScreen />;`

### Tests (`frontend/__tests__/expenses/`, 6 files / 32 tests, all NEW)

| File | Tests | Covers |
|---|---|---|
| `readiness.test.ts` | 8 | Both flag shapes, null, unknown severity, label mapping, `suggestedFix` |
| `submit-gate.test.ts` | 3 | Green/amber/red gate mapping |
| `report-list.test.tsx` | 6 | Status badges, "needs review" emphasis, amounts, default sort params, client search, `onSelect` |
| `entries-table.test.tsx` | 5 | `match_source` labels, policy column from entry status, amounts from DTO, lineage expansion (auto + null) |
| `submit-bar.test.tsx` | 7 | Gate states, amber → modal, green → mutate with id+version, non-draft renders nothing, 409 CONFLICT error toast, success toast |
| `rejection-notice.test.tsx` | 3 | Flags with citation, honest gap note, "No reason recorded" |

---

## KEY DECISIONS

1. **Submit gate from severity ONLY** — `computeSubmitGate(level)`. All flags green →
   enabled; any advisory/medium/low → amber + warning-confirmation modal; any
   blocking/high → red + disabled + tooltip. Totals read straight from
   `report.total_amount`; no policy re-eval and no client total recompute anywhere
   in the new code.

2. **Assemble = disabled with flagged note** — No REST Expense-Agent trigger exists.
   The Expense Agent assembles only via the conversational SSE path
   (`POST /conversations/{id}/messages {payload.trip_id}`) = the AI-assistant
   surface (P5-T10). `POST /expense-reports` creates a manual draft (no agent, no
   flags). Assemble button disabled with visible text note: "Assembly runs in the AI
   assistant (P5-T10)". Aligns with the no-trigger + frontend-only fences.

3. **Rejection = `pre_submit_flags` + honest gap note** — `approval_steps.reason`
   exists in the DB but the approval API is `POST + PATCH` only, both manager-gated,
   no GET, no report-level decision field. No employee-readable path. Rejection notice
   shows status + policy flags as "Issues to resolve" (with clause citations) + an
   honest note that the reviewer's written comment is not exposed by the current
   employee API. "No reason recorded" when flags are also empty. Never fabricated.

4. **`ReportStatusBadge` on the generic `Badge` primitive** — The existing `StatusBadge`
   primitive has exactly five variants: `in-policy/out-of-policy/pending/approved/
   rejected` (policy/approval semantics, fixed labels). It cannot express
   `draft/submitted/reimbursed`. Built a feature-local `ReportStatusBadge` on the
   generic `Badge` primitive, mirroring the `statusVariant`/`tripStatusVariant`
   pattern already in the codebase. `StatusBadge` is still used for policy/match
   semantics. No new design-system primitive; token classes only.

5. **Assemble note is visible text, not a tooltip** — The `AtlasButton` `disabledReason`
   prop wraps in a `TooltipTrigger` (type="button"), creating nested `<button>`
   elements when the button is always visible. The visible note div avoids this.
   The red-submit `disabledReason` tooltip is retained (appears only on flagged
   drafts, not always-visible, correct UX).

---

## RECONCILE / DEVIATIONS FROM DESIGN DOCS (code wins)

| Item | Design assumption | Reality (from code) |
|---|---|---|
| Assemble trigger | POST endpoint runs Expense Agent | GAP: no HTTP trigger; agent assembles via conversational SSE only. `POST /expense-reports` = manual draft. |
| List row policy summary | Policy/flag summary in list row | List DTO (`ReportListItem`) carries no policy/flag summary field. "Needs your review" emphasis from `status` only. |
| Seeded entries | Linked receipts / card transactions | All seeded entries have `null match_source`, `null trip_segment_id`, `null vendor` (text in `description`). UI degrades gracefully: lineage shows "Manually entered". |
| `pre_submit_flags` shape | One known shape | Two real shapes: agent `{flags:[{severity:"blocking"\|"advisory", reason, source_clause_ref, entry_ref, flag_type}]}` vs seed `{warnings:[{severity:"high", message, clause_ref}]}`. Single defensive normalizer. |
| Rejection reason | Reviewer text shown to employee | GAP: `approval_steps.reason` exists but approval API is POST+PATCH, manager-gated, no GET, no report-level decision field. No employee-readable path. Falls back to `pre_submit_flags` + honest note. |
| Image serving | Receipt images viewable | GAP (NFR-SEC4) carried forward: `image_url` is an opaque object-store key with no scoped serving endpoint. Lineage shows metadata refs, never an image. |
| Money field type | `number` | Wire is `string` (Pydantic v2 serializes `Decimal` → JSON string, e.g. `"1351.00"`). DTO types corrected to `string`; `formatCurrency` coerces. |

---

## ISSUES FOUND & VERIFICATION-ENV NOTE

- **Stale backend container**: The running `atlas-backend` image predated the
  org-chart + expense-report seed steps (`_seed_expense_reports_and_approvals`,
  `_seed_org_chart_extras`). The API is current; only the baked seed script is old.
  The demo org was seeded by piping the current `seed_all.py` via stdin into the
  running container (`docker exec -i atlas-backend python - < database/seeds/seed_all.py`).
  No repo/backend change was made.
- **G5 consumed the draft**: `PATCH /expense-reports/{id}` for verification submitted
  "June Miscellaneous" (R2, the only draft). Re-seed a fresh DB to restore a
  submittable draft for the live browser walkthrough.
- **Recommended before next demo**: rebuild the backend image (`docker compose build
  backend`) and re-run the seed.

---

## VALIDATION EVIDENCE

### Static checks

| Check | Result |
|---|---|
| `npx tsc --noEmit` | 0 errors |
| `next lint` / ESLint | Clean (✔ No ESLint warnings or errors) |
| `next build` | ✓ `/expenses` route at 11.2 kB / 167 kB first load |
| `npx vitest run` | 110/110 pass (32 new expense tests) |

### Grep hygiene (new code, all clean)

| Gate | Check | Result |
|---|---|---|
| G8 | No client sum of `amount` fields | ✓ |
| G9 | No client match/categorize/policy-eval/itemize | ✓ |
| G10 | No object-store/MinIO/presigned URL constructed client-side | ✓ |
| G11 | No token logging; no `NEXT_PUBLIC_*SECRET\|KEY\|TOKEN` | ✓ |
| G12 | Zero raw hex colors; token classes only | ✓ |

### Live curl (employee account; credential read from `.env`, bearer token not printed)

| Gate | Endpoint | Result |
|---|---|---|
| G1 | `GET /expense-reports` | `Page` envelope, DTO mirrors match; `total_amount: "1351.00"` (string) |
| G2 | Owner-scoping | Employee sees exactly 3 own reports; employee2's 2 absent |
| G3 | `GET /expense-reports/{id}/entries` | `match_source/status/trip_segment_id` straight from DTO |
| G4/G7 | Rejected report `pre_submit_flags` | `{warnings:[{severity:"high", message:"Lodging rate exceeds policy limit", clause_ref:"§4.2"}]}`; reviewer reason absent (gap confirmed) |
| G5 | `PATCH` correct `If-Match` | `200`, `status: "submitted"`, version 1→2, `submitted_at` set |
| G6 | `PATCH` stale `If-Match: 0` | `409 {"error":{"code":"CONFLICT","message":"Record was modified concurrently; reload and retry.",...}}` |

---

## DEFERRED / FLAGGED

- **Backend gap (assemble)**: No HTTP Expense-Agent trigger → assemble belongs to the
  AI assistant surface (P5-T10). The Validate gate's "assemble" half is blocked at
  the backend; "review pre-submit flags → submit" and "rejection reasons display"
  are fully delivered.
- **Backend gap (rejection reason)**: A scoped `GET /approvals?subject=...` or a
  report-level `latest_decision` field on the expense DTO would close US-E5 AC2.
  Until then, `pre_submit_flags` + honest note is the fallback.
- **Image serving gap (NFR-SEC4)** carried forward — no scoped receipt-image serving
  endpoint.
- **Infra**: Rebuild backend image + re-seed before the next live demo.
- **Approval decision surface** → P5-T8 (next).
