# Phase 12 follow-ups — UI polish left over from the browser QA runs

Branch `fu/uinits` (base `main` at 962e71a, alembic head `0023_entry_release_lineage`,
**no migration added**). Ten small items, each with a test that fails without the fix
(proven by mutation: revert, see red, restore).

## What was built, and why

| # | Item | Fix | Where |
|---|------|-----|-------|
| 1 | A throttled sign-in (429) showed both the inline alert and the red "You're going a little fast" toast; after the countdown "You can try signing in again now." was still red. | The global toaster skips routes that render the 429 inline (`INLINE_RATE_LIMIT_ROUTES = ["/login"]`). The login page shows the countdown once (inline alert) and, when the wait is over, a neutral `role=status` note. | `components/ui/rate-limit-toaster.tsx`, `app/(auth)/login/page.tsx` |
| 2 | "Don't use an organisation code" looked like grey helper text. | Both organisation toggles share one link style: primary colour, underlined, focus ring, disabled state. | `app/(auth)/login/page.tsx` |
| 3 | Client file names with bidi / control characters (e.g. U+202E) rendered reversed or garbled. | `lib/receipts/display-name.ts` (`displayText`, `displayFileName`) strips U+202A–202E, U+2066–2069, zero-width characters, BOM, soft hyphen and C0/C1 controls, collapses whitespace, truncates with "…" while keeping the extension. It is applied to the receipt queue, image preview (receipts screen, entry receipts, viewer), delete confirm, upload-zone rejection toast, viewer dialog title and summary, entry-editor receipt summaries and approval-side receipt cards (OCR vendor text). | display only |
| 4 | An entry kept "Match: Auto · Receipt" after its receipt was removed. | Fixed at the source. `expense/service.reported_match_source` makes a receipt-sourced entry with no direct or shared receipt report `match_source = null` ("Unmatched"). It is applied in `entries_with_receipt_ids`, the one serializer behind every entry view. | `backend/app/modules/expense/service.py` |
| 5 | Add-mileage showed no amount until after confirming. | New read-only `GET /api/v1/mileage-rate` (permission `expense.update.own`, the same gate as the POST) returns the rate `current_mileage_rate` charges, plus currency and clause. The dialog shows a live estimate ("$130.50 = 180.0 business mi × $0.725/mi (rate MIL-01)"), or the current rate before a distance is entered. | `expense/router.py`, `policy_fields.py`, `add-mileage-dialog.tsx` |
| 6 | The finance approvals exception-type counts ignored the other active filters. | `GET /approvals/exception-types` takes the same filter params as `GET /approvals` and counts within that set (faceted: the `exception_type` param itself and the paging keys are ignored). The frontend sends the other URL filters and keeps the previous counts while refetching. | `approval/filter_options.py`, `router.py`, `approval-filter-hooks.ts`, `approvals-screen.tsx` |
| 7 | Row action buttons wrapped onto two lines on narrow widths. | `components/atlas/row-actions.tsx` keeps them on one line (`flex-nowrap`, `whitespace-nowrap`) and below `sm` (max-width 639px) folds them behind one "Actions for …" toggle that expands them in place. Used by the report entries table, the trip approval panel (trips list) and the users table. | |
| 8 | The Policy page's "Policy document" pane showed the legacy stub's 8 sections. | `GET /policies` gains an optional `document`: the org's current corpus *policy* document (Helios T&E) as sections → clauses (code + text) in document order, read from the indexed corpus units. The page renders it with clause-code badges and falls back to `document_text` when it is absent. | `policy/document.py`, `policy/router.py`, `components/policy/policy-document.tsx` |
| 9 | The finance admin's own grade lock ("another finance admin must") was a dead end when they were the only one. | On your own row the dialog checks the number of active finance admins (`GET /users?role=finance_admin&status=active&size=1`). When the total is 1 it explains why the lock exists and how to resolve it: add a Finance admin, or promote an existing user and ask them to set your grade. | `components/users/user-dialogs.tsx` |
| 10 | Refused uploads: show the real server message only; never auto-retry a 429. | `uploadErrorMessage` shows the server's own `PAYLOAD_TOO_LARGE` message when the 413 has an envelope; only a proxy's bare 413 gets the client copy. Tests go through the real upload queue and app QueryClient: 413/415/422 show exactly the server text (no "remaining" or quota wording) and are not retryable; a 429 sends one request, and only an explicit Retry sends again. | `lib/receipts/validation.ts` |

## Key decisions

- **#1** The toaster reads `window.location.pathname` when the event fires, not a router hook. It stays one global mount and needs no navigation context. Other routes are unchanged.
- **#3** Sanitising is **display only**. The `File`, the upload and everything stored keep the original name. `validation.fileExtension` still mirrors the server's own parser. A name made only of invisible characters renders as "Untitled file".
- **#4** The value is derived at read time and the stored column is **not** rewritten, because the receipt inbox (`single_receipt_inbox_lines`) and the discard-upload path read it as "how the line was created". No migration. `manual`, `card_txn` and `trip` pass through unchanged, and a receipt shared from an itemized sibling counts as a current link.
- **#5** The estimate is display only. The POST body is unchanged (trip only, never an amount or a rate) and the stored figure is always the server's. If the rate cannot be read, no estimate is shown. The contract registry now lists 69 routes.
- **#6** The count query reuses `scoped_filtered_approvals`, the list's own scope and filter builder. A count therefore always equals the list total for the same filters. Filters only AND onto `scope_query`, so a manager never sees another team's counts. Malformed or unknown filters return the list's own 400 envelope.
- **#7** Folded actions stay **mounted** (both the `hidden` attribute and the `hidden` class, because Tailwind's `flex` beats `[hidden]`). Parent-owned dialogs and state survive the toggle. The expanded list sits in normal flow, not a floating popover, so a table's horizontal-scroll container can't clip it. Without `matchMedia` (SSR, unit tests) the actions render inline, so the first paint and the existing tests are unchanged.
- **#8** The source is the indexed corpus that the existing idempotent reindex boot step (`docker/entrypoints/backend-entrypoint.sh` → `database.seeds.reindex_policy_corpus`: `_seed_corpus` refreshes the text, `_embed_corpus` re-chunks) keeps current on every deploy. That is why no seed change, migration or new boot step was needed, and why the stub policy row and its rules (the rule engine's source) are untouched. Retrieval content is only **read**. eval-db stays at exactly 48 passed.
- **#9** The segregation-of-duties rule is not weakened. The select stays disabled, the grade is never sent, and the server still refuses a self change. The lookup only runs on your own row.
- Shared hot files got small, localised edits: `expense/service.py` (+1 helper, +1 line), the users page (separate two-line commit), the login page (two separate small commits) and the upload zone (one line).

## Limits

- **#1** Only `/login` is listed as an inline-429 route. Any future screen with its own countdown must add itself to `INLINE_RATE_LIMIT_ROUTES`.
- **#4** The badge now says "Unmatched" for such a line. The entry-lineage note "Linked from an uploaded receipt." (a stored `receipt` source with no receipt) can no longer appear from the API.
- **#7** In the folded menu the icon-only buttons (attach, delete, edit-user) still show only their icons. Their accessible names come from the existing `aria-label`s.
- **#8** `GET /policies` itself still picks the active policy row with an unordered `.first()` (pre-existing: the stub and four corpus rows are all `is_active`). The header line and `document_text` come from that row. The new `document` field is deterministic (the latest `policy`-type corpus row). If the corpus has not been indexed yet (no units), the pane falls back to the stored text.
- **#9** The hint depends on a `GET /users` total. If that request fails, the short hint is shown.
- **#10** The per-minute receipt budget still counts refused uploads on the server (the limiter runs before the route). That is a backend design choice and is unchanged. The client simply never surfaces `X-RateLimit-Remaining`.

## How to verify

Python tests run through an isolated pytest runner (local providers, a dedicated test database):

```bash
R=<your isolated pytest runner, e.g. .venv/bin/python -m pytest with the test DB env set>
$R -q backend/tests/test_fu_match_source_db.py backend/tests/test_fu_mileage_rate_db.py \
      backend/tests/api/test_fu_exception_counts.py backend/tests/test_fu_policy_document_db.py
$R -q backend/tests/api/test_contract_matrix.py      # 69 live routes
$R -q backend/tests/eval -m db                       # exactly 48 passed
cd frontend && npx vitest run __tests__/followups    # one file per item
```

In the browser: trigger a sign-in 429 (one message, then a neutral note). Upload `invoice‮gnp.exe`-style names (they render cleaned). Remove an auto-drafted entry's receipt (the badge reads "Unmatched"). Open Add mileage and type a distance (the estimate updates live). Filter approvals by department (the exception counts change). Narrow the window under 640px on a report, the trips list and users (one "Actions" toggle per row). Open Policy & Admin (Helios sections with clause codes). Edit your own row as the sole finance admin (the explanatory hint appears).
