# Phase 9 — Product gaps: team visibility, finance filters, CSV exports (gaps 5, 6, 8)

Track branch `pg/team`, base `2fbe114` (alembic head `0015_canonical_policy_figures`).
**No migration** — revision id `0021_*` was reserved for this track and is not used (see "Indexes").

## What was built

### Gap 5 — manager Team view (PRD US-M3)

* **API** (`backend/app/modules/team/`, mounted at `/api/v1/team`):
  * `GET /team/members` — paginated team summaries: name, department, role, open trips
    (draft + pending_approval + approved), reports by status (all five statuses, zero-filled),
    pending approvals on the member's submissions, month-to-date and trailing-90-day spend
    (Decimal strings), the org base currency, and how many foreign-currency reports were left
    out. `?q=` (name / department substring, ≤100 chars), `?sort=` (allow-listed: name,
    department, open_trips, pending_approvals, spend_mtd, spend_90d; `-field` / `field:desc`),
    `?page`/`?size`. Unknown keys → 400 (deny-by-default `parse_filters`).
  * `GET /team/members/{user_id}` — the summary plus the member's trips and reports as two
    `Page`s (`?trips_page`, `?reports_page`, `?size`).
* **Who is on the team** — ONE definition, the existing read scope: a manager's team is
  `rbac.direct_report_ids` (the helper `scope_query` uses: one level, same org, manager not
  included); finance_admin → every user of the organization (finance's org-wide read scope);
  any other role → 403 (fail closed). The router gate needs `expense.read.reports` (manager)
  OR `expense.read.org` (finance) — an employee holds neither → 403. Anyone outside the
  caller's team (same-org stranger, the manager themself, another tenant, a random id) → 404.
* The drill-down lists call the existing `travel_service.list_trips` / `expense_service.list_reports`
  with an owner filter, so they are `scope_query`'d exactly like `/trips` and `/expense-reports`.
* **UI**: nav item **Team** (`UsersRound`, gated on the same two permissions — managers and
  finance), `/team` (search, sort select + sortable columns, pagination, result count, empty
  state, foreign-currency footnote) and `/team/[id]` (KPI cards, Trips / Reports tabs with
  counts, per-tab pagination and empty states). Trips open in the existing `TripApprovalPanel`
  (read-only banner with the member's name, owner actions hidden by `isRecordOwner`) with the
  segment list; reports open the existing `ReportDetail` (read-only for a non-owner). The
  manager's own `/expenses` and `/trips` are untouched (still `mine=true`).
* Manager mobile tab bar: Dashboard, Expenses, Approvals, Team (Trips stays in the sidebar).

### Gap 6 — finance filters (PRD US-F2 AC1)

* `backend/app/modules/approval/filters.py` — the canonical **exception-type catalog** and the
  allow-listed filter predicates, applied AFTER `scope_query` (they only AND a predicate, so a
  filter can narrow but never widen a manager's or a tenant's scope — proved by a test that
  goes red when the order is broken).
* **Approvals** (`GET /approvals`): `exception_type`, `department` (the submitter's,
  case-insensitive), `owner_id`, `submitted_from` / `submitted_to` (approval created = entered
  the queue), `decided_from` / `decided_to` (ONE decided approval step inside the range — a
  single EXISTS, so steps straddling the range do not match),
  `amount_min` / `amount_max` (polymorphic: report total / trip estimated cost). Dates are
  strict `YYYY-MM-DD`, inclusive (`[from 00:00Z, to+1 00:00Z)`); money is Decimal with ≤2
  decimals; `from > to` → 400. List rows now also carry `submitter_department` and
  `exception_types`.
* **Expense reports** (`GET /expense-reports`): `exception_type` (pre-submit flags),
  `department` (owner's), `submitted_from` / `submitted_to` (`submitted_at`), `amount_min` /
  `amount_max` (`total_amount`). Pagination totals are computed after the filters.
* **Filter options**: `GET /approvals/exception-types` (every canonical type with the count of
  approvals IN THE CALLER'S SCOPE carrying it — counted with the very predicate the filter
  applies) and `GET /approvals/departments` (departments of the users the caller can read,
  `scope_query` on users). Both need `approval.read`.
* **UI**: a filter bar above the approval queue (status, type, exception type with counts,
  department, submitted from / to), URL-synced (`router.replace`, allow-listed + shape-checked
  parsing so a hand-edited URL never sends a malformed value), "Clear all", result count
  ("N results · showing the first 100 · k filters applied"), loading and filtered-empty states.

### Gap 8 (CSV export part)

* `backend/app/core/csv_export.py` — RFC-4180 (CRLF, QUOTE_MINIMAL), UTF-8 BOM, formula-
  injection guard (text cells starting with `=` `+` `-` `@` TAB CR get a leading `'`; only
  server-formatted Decimal / timestamp / boolean cells skip it), hard cap
  (`EXPORT_MAX_ROWS = 5000`; `cap + 1` rows are read, a `# TRUNCATED` marker row is appended
  and `X-Export-Truncated: true` / `X-Export-Row-Count` headers are set), dated filename
  `atlas-<stem>-YYYY-MM-DD.csv`, streamed body.
* `GET /approvals/export.csv` — finance-only (`approval.read` + `expense.read.org`), the SAME
  scope + filters + order as the list (`service.scoped_filtered_approvals` is shared).
  Columns are on-screen fields only: submitted at, status, subject type, subject, submitter,
  department, amount, currency, risk level, auto-approved, exception types, last decided at —
  no internal ids, no emails.
* `GET /audit/export.csv` — finance-only (`audit.read`), same org scope / filters
  (action, entity_type, entity_id, actor_id) / sort as `GET /audit`; columns time, actor,
  action, entity type, entity id (what the audit table shows; before/after JSON is not
  exported).
* Every export is itself audit-logged (`approvals.exported` / `audit.exported`, entity type
  `export`, actor, filters, sort, row count, truncated) in the same transaction; the audit
  export reads its rows first, so the file never contains its own export event. A 403 or a
  400 writes nothing.
* **UI**: "Export CSV" next to the approval filters (finance only) and next to the audit
  filters — an authenticated `fetch` → blob download (`lib/api/download.ts`, bearer token,
  one shared-refresh retry on 401), dated filename, error toast on failure.

## Key decisions

* **Team = existing scope helpers.** No new "team" query: manager → `direct_report_ids`,
  finance → org (scope_query's finance branch). Finance is exposed (org-wide) because it is
  consistent with finance's existing read scope.
* **Spend** follows the manager dashboard's definition (approved + reimbursed report totals),
  dated by `submitted_at` (else `created_at`). Atlas has no FX conversion, so only reports in
  the org base currency are summed and the excluded count is returned and shown — never a
  silent mixed-currency sum.
* **Exception types** are defined from the data producers: every `flag_type` the pre-submit
  check / approval agent emits, every `PolicyRuleType` (rule verdict flags carry
  `rule_type`), and `out_of_policy_segment` (a trip segment row `in_policy: false`). A
  hermetic test scans the agent source for `flag_type="…"` / `*_FLAG = "…"` and fails if a
  new producer flag is not in the catalog. Values outside the catalog → 400.
* An approval matches an exception type when its `risk_summary` OR (for a report subject)
  its report's `pre_submit_flags` carry it — JSONB `@>` with a bound parameter (same pattern
  as `refresh_duplicate_peers`), never string-built SQL. The Python twin
  (`exception_types_of`) labels rows / CSV cells with the same three rules.
* "Submitted" for approvals = `Approval.created_at` (when it entered the queue); a resubmitted
  report keeps its original approval row. Reports use `submitted_at`.
* Export permission: approvals export needs `expense.read.org` on top of `approval.read`
  (the org-wide reader marker only finance holds); audit export reuses `audit.read`.
* Exports are rendered from rows read inside the request and then streamed (the DB session is
  not used while the body streams). Cap 5000 rows keeps memory bounded.
* Contract matrix: 6 new routes registered (41 → 47); the count assertion in
  `test_contract_matrix.py` was updated because the live surface genuinely grew.
* Hot files touched minimally: `approval/service.py` (allow-list + one shared query builder +
  two list-row fields), `expense/service.py` (allow-list + one extended-filter call),
  `api/v1.py` (one router), `nav-config.ts` (one item + manager mobile tabs),
  `lib/types/approvals.ts` (additive optional fields / params). New logic lives in new
  modules.

## Limits / known gaps

* Report-list filters (gap 6) are API-only: there is no finance-wide reports list screen
  (`/expenses` is deliberately personal). The approvals queue is the finance review surface.
* No amount / owner / decided-date controls in the filter bar (the API supports them).
* Team list sorting/search is computed in Python over the caller's team (one grouped query
  per metric); fine for team / demo-org sizes, not for very large orgs.
* `exception-types` runs one scoped count per catalog type (17 small queries).
* No GIN index on `approvals.risk_summary` / `expense_reports.pre_submit_flags`, so JSONB
  containment scans the tenant's rows. Suggested future migration (0021, index-only,
  `CREATE INDEX CONCURRENTLY … USING gin (… jsonb_path_ops)`) if volume grows.
* The approval queue still loads at most 100 rows; the count line says when more match.
* `next build` was not run (the root layout loads `next/font/google`, which needs network).

## How to verify

```bash
R=<local-runner-dir>/pg/run_team.sh   # isolated DB, local providers
$R -q -m db backend/tests/api/test_pg_team_view.py backend/tests/api/test_pg_finance_filters.py backend/tests/api/test_pg_csv_export.py
$R -q -m 'not db' backend/tests/test_pg_team_unit.py backend/tests/test_pg_filters_unit.py backend/tests/test_pg_csv_unit.py
cd frontend && npx vitest run __tests__/product-gaps
```

Manual: as `manager@atlas-demo.internal` open **Team** → a direct report → Trips / Reports
(read-only banners, no owner actions). As `finance@…` open **Approvals**, pick an exception
type / department / dates, copy the URL into a new tab (same view), **Export CSV**; open
**Audit** → filter → **Export CSV**; the audit log then shows `approvals.exported` /
`audit.exported` entries.

Guards proven red once (temporarily reverted, then restored): manager team = org-wide;
filters applied without `scope_query`; CSV injection prefix removed; the `expense.read.org`
export gate removed; export audit write removed; the row cap removed; a catalog entry
removed; the team permission gate removed; the frontend Team nav permission gate removed;
the URL exception-type shape check removed. The decided-range test was written red
against the first (two-EXISTS) version and fixed in its own commit.
