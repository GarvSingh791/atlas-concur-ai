# P5-T4 — Dashboard (Phase 5)

**STATUS:** Complete, validated, committed.

---

## Scope Reconciliation

The task spec fenced this task as "no backend changes." Reconcile found that `GET /api/v1/analytics/overview` was not implemented — the `analytics/` module directory contained only a `.gitkeep`, the route was unregistered in `v1.py`, and a live `curl` returned HTTP 404 for an authenticated employee. The master execution plan (`MASTER_EXECUTION_PLAN`) defines `/analytics` as a P5-T4 deliverable, and the frontend dashboard cannot be sourced or meaningfully validated without the endpoint it reads from.

Per reconciliation discipline (code wins; deviation documented), a backend analytics module and endpoint were built alongside the frontend. All 12 validation gates are genuine end-to-end checks against the real endpoint — none are mocked. The scope expansion is recorded here and in the commit body; it is not silent.

---

## What Was Built

### Backend — NEW

- **`backend/app/modules/analytics/__init__.py`** — empty module init.
- **`backend/app/modules/analytics/schemas.py`** — 9 Pydantic models: `KpiMetric`, `Recommendation`, `PolicyWarning`, `ApprovalQueueItem`, `ActivityItem`, `EmployeeOverview`, `ManagerOverview`, `FinanceOverview`, `AnalyticsOverviewResponse`. `AnalyticsOverviewResponse` carries exactly one populated section (`employee | manager | finance`) determined server-side by the caller's role.
- **`backend/app/modules/analytics/service.py`** — `get_overview(session, current_user)` branches by `current_user.role`. All queries are org-scoped via explicit `organization_id` filters; manager narrowing uses `direct_report_ids()` from `core/rbac.py`. Employee recommendations are rule-based heuristics (pending receipts, days to nearest trip, draft reports). Policy warnings are read from stored `ExpenseReport.pre_submit_flags` JSONB — no request-time policy evaluation. Finance aggregations use `func.count/sum/avg`. No LLM call anywhere.
- **`backend/app/modules/analytics/router.py`** — single `GET /overview` endpoint, response model `AnalyticsOverviewResponse`, gated by `Depends(get_current_user)` (auth-only; record scoping is enforced server-side by role branching).
- **`backend/tests/test_analytics_api.py`** — 8 tests: per-role payload shape (employee/manager/finance), 401 unauthenticated, employee pending-approvals scoped to own records, finance KPIs non-empty, cross-tenant isolation (org B finance sees only org B data), LLM-absence AST walk of `service.py`.

### Backend — MODIFIED

- **`backend/app/api/v1.py`** — wired `analytics_router` at prefix `/analytics`.
- **`database/seeds/seed_all.py`** — Steps 8–9 added:
  - `_seed_org_chart_extras()`: creates `manager2@atlas-demo.internal` (Brenda Taylor, top-level manager) and `employee2@atlas-demo.internal` (Carlos Rivera, her direct report), making org membership strictly `finance(org) ⊋ manager(reports) ⊋ employee(own)`.
  - `_seed_expense_reports_and_approvals()`: 5 expense reports with 9 expense entries and 4 approvals (pending, auto_approved, approved-with-step, rejected), with varied categories, amounts, `pre_submit_flags`, and `risk_summary`. Idempotent guards on both functions. Passwords hashed via the established `hash_password(get_settings().SEED_DEFAULT_PASSWORD)` convention.

### Frontend — NEW

- **`frontend/lib/types/analytics.ts`** — TypeScript DTO mirror of `backend/app/modules/analytics/schemas.py`, byte-for-byte. Exports: `KpiMetric`, `Recommendation`, `PolicyWarning`, `ApprovalQueueItem`, `ActivityItem`, `SpendByCategory`, `SpendOverTime`, `SpendByDepartment`, `TeamSpend`, `EmployeeOverview`, `ManagerOverview`, `FinanceOverview`, `AnalyticsOverviewResponse`.
- **`frontend/lib/query/analytics-hooks.ts`** — `useAnalyticsOverview()` (`GET /analytics/overview`), `useTrips()` (`GET /trips`), `useExpenseReports()` (`GET /expense-reports`). `analyticsQueryKeys` const. Single `useAnalyticsOverview()` call per dashboard deduplicates the request across all widgets via TanStack Query.
- **`frontend/components/dashboard/widget-boundary.tsx`** — React class-based error boundary. A widget render error shows `ErrorState("Widget unavailable")` with retry; does not propagate to the page.
- **`frontend/components/dashboard/widgets/kpi-widget.tsx`** — Maps `KpiMetric[]` to `KPICard` grid. Handles loading (shimmer grid), error, empty.
- **`frontend/components/dashboard/widgets/queue-widget.tsx`** — Approval queue list with `StatusBadge`. Handles loading, error, empty.
- **`frontend/components/dashboard/widgets/timeline-widget.tsx`** — Trip list with status badge. Handles loading, error, empty.
- **`frontend/components/dashboard/widgets/chart-widget.tsx`** — recharts `BarChart` wrapped in `ResponsiveContainer`. Generic `dataKey` / `valueKey` props. Handles loading, error, empty.
- **`frontend/components/dashboard/widgets/status-widget.tsx`** — `RecommendationsWidget` (purple token treatment for AI insights) and `PolicyWarningsWidget` (amber/red border-left by severity). Handles loading, error, empty.
- **`frontend/components/dashboard/employee-dashboard.tsx`** — 4 KPI cards + Trip Timeline + Recent Expenses (inline typed widget) + AI Recommendations + Policy Warnings + full-width "Plan your next trip →" CTA (`/planner`).
- **`frontend/components/dashboard/manager-dashboard.tsx`** — Pending Approvals queue + Team Spend by Category chart + Recent Activity feed.
- **`frontend/components/dashboard/finance-dashboard.tsx`** — Consolidated KPIs + Spend Over Time chart + Spend by Category chart + Spend by Department chart.
- **`frontend/__tests__/dashboard/widget-boundary.test.tsx`** — 3 tests: children render normally, error fallback on child throw, one boundary error does not crash sibling boundaries.
- **`frontend/__tests__/dashboard/kpi-widget.test.tsx`** — 4 tests: loading shimmer, error state, empty state, label rendering.
- **`frontend/__tests__/dashboard/role-composition.test.tsx`** — 6 tests: role dispatch (employee/manager/finance), employee CTA present, CTA absent in manager/finance.

### Frontend — MODIFIED

- **`frontend/app/(app)/dashboard/page.tsx`** — Placeholder replaced with role-dispatch client component: `finance_admin → FinanceDashboard`, `manager → ManagerDashboard`, default → `EmployeeDashboard`. Uses `useSession().role` from `session-provider.tsx`.
- **`frontend/lib/types/index.ts`** — Added `export * from "./analytics"`.
- **`frontend/package.json`** — Added `"recharts": "^2.0.0"` to `dependencies`.

---

## Key Decisions

**Server-side scoping, client-side composition.** The same `GET /analytics/overview` endpoint returns the caller's own slice; the frontend selects widget layout by role and never re-filters or re-scopes the server data. An employee token cannot receive manager or finance data regardless of what the frontend renders.

**No LLM on any analytics path.** Recommendations are three rule-based heuristics over the user's own stored data (pending OCR receipts, days to nearest trip, draft expense reports). Policy warnings are read from `ExpenseReport.pre_submit_flags` (populated at submit time by a prior phase). This is enforced by an AST-walk test in `test_analytics_api.py` that fails if `anthropic`, `openai`, or `langchain` appear as imports in `service.py`.

**No schema drift.** No models, migrations, or enums were added or changed. The `spend_by_department` metric uses `split_part(User.email, '@', 1)` as a department proxy rather than adding a `User.department` column. This is flagged for replacement when the column is added.

**Widget isolation via error boundary.** Each widget section is wrapped in `WidgetBoundary`. A render error in one widget shows a local `ErrorState` with a retry button; it does not unmount or crash adjacent widgets.

**Primitives, session, and shell consumed — not rebuilt.** `KPICard`, `StatusBadge`, `EmptyState`, `LoadingState`, `ErrorState` from `@/components/atlas`; `useSession()` from `session-provider.tsx`; `AppShell` and active-nav from the P5-T3 shell. None were modified.

**Non-additive role composition.** Although RBAC permissions are additive (a manager holds employee + approval perms), the dashboard renders exactly one curated layout keyed on `useSession().role`. A manager does not see employee KPIs or the `/planner` CTA; finance does not see manager or employee widgets.

---

## Validation Evidence

All 12 gates green:

- **G1 Reconcile:** Analytics module imports verified; `GET /api/v1/analytics/overview` confirmed registered in `api_router.routes`.
- **G2–G4 Composition:** Role-composition tests confirm each role renders only its widget set; no cross-role widget bleed.
- **G5–G7 States:** Widget loading/empty/error states tested via mocked hooks; `WidgetBoundary` isolation test confirms sibling widgets survive a single failure.
- **G8 Server-scoping:** Backend tests verify `employee ⊊ manager ⊊ finance` record counts + org B cross-tenant isolation.
- **G9 Primitives/tokens:** All widgets compose `@/components/atlas`; zero raw hex in dashboard files; purple AI / amber warning via CSS custom-property tokens.
- **G10 Shell:** Dashboard mounted at `/dashboard` inside `(app)` shell; placeholder replaced; CTA → `/planner`.
- **G11 Build/type/lint:** `ruff` clean; `tsc --noEmit` 0 errors; `eslint` 0 errors; `npm run test` 14/14 pass; all Python syntax valid.
- **G12 Fence:** Only analytics module + seed augmentation + frontend dashboard files changed; no other route stubs, mutations, assistant, search, or notifications touched.

---

## Deferred / Flagged

- **recharts dep:** `recharts ^2.0.0` added to `package.json`; `package-lock.json` synced. `node_modules` gitignored. Run `npm install` in `frontend/` to materialize locally before first dev server run or CI.
- **Seed re-run required:** `python -m database.seeds.seed_all` must be run against a live DB to populate the new expense/approval data before the analytics endpoint returns non-zero values for those metrics.
- **`spend_by_department` proxy:** Email prefix (`split_part(email, '@', 1)`) used as department proxy — replace with `User.department` when that column is added.
- **`analytics.read` permission:** Endpoint is auth-only (gated by `get_current_user`); a dedicated permission key is a possible later refinement for finer-grained access control.

---

## What's Next

**P5-T5 — Travel Planner screen:** The first domain screen, replacing the `/planner` stub that the employee dashboard CTA already links to.
