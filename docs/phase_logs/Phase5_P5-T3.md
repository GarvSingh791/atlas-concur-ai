# P5-T3 — App Shell (Phase 5)

## STATUS

Complete, validated, uncommitted at time of writing.

---

## WHAT WAS BUILT

### Role-aware Sidebar (`frontend/components/atlas/sidebar.tsx`)
Declarative permission→item nav config (in `frontend/lib/nav-config.ts`) filtered at render time against the P5-T2 session (`useSession`). Renders brand area (Compass icon + "Atlas" wordmark linked to `/dashboard`) and a role badge below it when expanded. Active-state indicator: 3px `var(--color-primary-600)` left border, flush with the nav item row. Hover transitions over 150ms (via Tailwind `transition-colors`). Collapsed 64px icon-only mode (`SidebarMode = "collapsed"`) with CSS-only tooltip wrappers (`group-hover/tooltip`) — no Radix compose-refs dependency to avoid the infinite-loop regression fixed in `button.tsx`. Pinned AI Assistant launcher at the bottom (Sparkles icon). On mobile the sidebar mounts as a fixed-position slide-in drawer (hamburger toggle), with a `bg-black/50` backdrop and close button.

### Top Bar (`frontend/components/atlas/top-bar.tsx`)
Sticky 56px header. Left: hamburger button (shown only when `sidebarMode === "hidden"`) + current page title derived from the active nav item. Right: presentational global-search field (full-width rounded input, no wiring — behavior deferred); notifications bell with a dot badge (presentational, no endpoint); account menu (Radix `DropdownMenu`) displaying the real session user name, email, and role badge from P5-T2 `useSession`. Sign-out calls the `useLogout` hook from `frontend/lib/query/auth-hooks.ts` (P5-T2).

### AI FAB + Assistant Slot (`frontend/components/atlas/ai-fab.tsx`, `frontend/components/atlas/assistant-drawer.tsx`)
48px gradient FAB (`from-[--color-ai-gradient-start] to-[--color-ai-gradient-end]`), fixed bottom-right on desktop/tablet, hidden on mobile. The FAB, the sidebar launcher button, and the global `Ctrl/Cmd + J` keyboard shortcut (registered in `useAppShell`) all toggle the same `isAssistantOpen` state. The assistant drawer is **EMPTY** — a placeholder heading and a close button only. No chat input, no message list, no SSE/EventSource. Full-screen on mobile (`inset-x-0 bottom-0 top-14`); panel (`w-[400px]`) on desktop.

### Responsive: Four Breakpoints
Breakpoints derived via `useMediaQuery` in `useAppShell`:

| Breakpoint | Width | Sidebar Mode |
|---|---|---|
| Desktop | ≥ 1280px | `expanded` — full 240px rail |
| Tablet | 1024–1279px | `collapsed` — 64px icon-only |
| Tablet (sm) | 768–1023px | `hidden` — hamburger drawer |
| Mobile | < 768px | `hidden` + bottom tab bar (MobileTabBar) |

The `MobileTabBar` (`frontend/components/atlas/mobile-tab-bar.tsx`) renders a role-curated subset of up to 4 nav items plus the Assistant tab, pinned to the bottom with `pb-safe` (CSS `env(safe-area-inset-bottom)` added to `globals.css`).

### Routable Placeholder Stubs
Nine new stubs (title-only, `EmptyState` component) created so nav links resolve and active-state tracking works end-to-end. The dashboard stub already existed from P5-T2.

| Route | File | Status |
|---|---|---|
| `/analytics` | `frontend/app/(app)/analytics/page.tsx` | NEW stub |
| `/approvals` | `frontend/app/(app)/approvals/page.tsx` | NEW stub |
| `/audit` | `frontend/app/(app)/audit/page.tsx` | NEW stub |
| `/expenses` | `frontend/app/(app)/expenses/page.tsx` | NEW stub |
| `/planner` | `frontend/app/(app)/planner/page.tsx` | NEW stub |
| `/policy` | `frontend/app/(app)/policy/page.tsx` | NEW stub |
| `/receipts` | `frontend/app/(app)/receipts/page.tsx` | NEW stub |
| `/trips` | `frontend/app/(app)/trips/page.tsx` | NEW stub |
| `/users` | `frontend/app/(app)/users/page.tsx` | NEW stub |

No stub fetches data. Each renders an `EmptyState` with a relevant icon and a "Coming in Phase 5" message.

---

## FILES CREATED / MODIFIED

### NEW
| File | Purpose |
|---|---|
| `frontend/lib/nav-config.ts` | Declarative nav catalog (`NAV_ITEMS`), `getVisibleNavItems()`, `getMobileTabItems()` |
| `frontend/lib/hooks/use-app-shell.ts` | Central shell state: sidebar mode, drawer open, assistant open, Ctrl/Cmd-J shortcut |
| `frontend/lib/hooks/use-media-query.ts` | SSR-safe `window.matchMedia` hook with hydration-safe default |
| `frontend/components/atlas/sidebar.tsx` | Role-aware sidebar + mobile drawer variant |
| `frontend/components/atlas/top-bar.tsx` | Top bar with search field, notifications, account menu |
| `frontend/components/atlas/app-shell.tsx` | Orchestrator composing Sidebar + TopBar + AssistantDrawer + AIFab + MobileTabBar |
| `frontend/components/atlas/ai-fab.tsx` | Gradient FAB button |
| `frontend/components/atlas/assistant-drawer.tsx` | Empty assistant slot (placeholder only) |
| `frontend/components/atlas/mobile-tab-bar.tsx` | Bottom tab bar for mobile |
| `frontend/app/(app)/analytics/page.tsx` | Placeholder stub |
| `frontend/app/(app)/approvals/page.tsx` | Placeholder stub |
| `frontend/app/(app)/audit/page.tsx` | Placeholder stub |
| `frontend/app/(app)/expenses/page.tsx` | Placeholder stub |
| `frontend/app/(app)/planner/page.tsx` | Placeholder stub |
| `frontend/app/(app)/policy/page.tsx` | Placeholder stub |
| `frontend/app/(app)/receipts/page.tsx` | Placeholder stub |
| `frontend/app/(app)/trips/page.tsx` | Placeholder stub |
| `frontend/app/(app)/users/page.tsx` | Placeholder stub |
| `frontend/.gitignore` | Frontend-scoped gitignore (env, `.next`, `node_modules`, media) |

### MODIFIED
| File | Change |
|---|---|
| `frontend/app/(app)/layout.tsx` | Replaced 91-line prototype shell (static hardcoded nav, static avatar) with a 10-line `<AuthGuard><AppShell>{children}</AppShell></AuthGuard>` delegation |
| `frontend/components/atlas/button.tsx` | Fixed `TooltipTrigger asChild` compose-refs infinite-loop regression by removing `asChild` and styling the trigger directly |
| `frontend/lib/hooks/use-toast.ts` | Fixed exhaustive-deps lint error: empty dependency array on the subscription `useEffect` (subscribe once on mount; prior `[state]` caused re-subscription on every state change) |
| `frontend/styles/globals.css` | Added `.pb-safe { padding-bottom: env(safe-area-inset-bottom); }` utility for mobile tab bar notch handling |

---

## KEY DECISIONS

### NAV-PERMISSION MODEL
Items in `NAV_ITEMS` carry optional `requiredPermissions` and/or `requiredRoles` gates. `getVisibleNavItems()` applies additive OR logic: an item is visible if it has no gates, **or** if the user holds any listed permission, **or** if the user holds any listed role. This mirrors the permission strings defined in P5-T2's `PermissionKey` union and `RoleKey`.

The Finance/Admin question (standalone vs. additive) was resolved as follows:
- **Employee-exclusive items** (`trip.create.own`, `expense.create.own`) are gated by permission only, so `finance_admin` — who lacks those permissions — correctly does not see Travel Planner, Trips, Receipts, or Expenses.
- **Management items** (Approvals, Analytics) carry both a permission gate *and* a role gate (`requiredRoles: ["finance_admin"]`), so both managers (via permission) and finance admins (via role) see them — additive access.
- **Administration items** (Policy, Users) are gated by permission (`policy.manage`, `users.read`). Audit is gated by role only (`finance_admin`).

This means Finance Admin sees: Dashboard + Approvals + Analytics + Policy + Audit + Users (6 items). Manager sees: Dashboard + Approvals + Analytics (and the employee items if they also carry those permissions — which they do). Employee sees: Dashboard + Planner + Trips + Receipts + Expenses (5 items).

### Nav Hiding Is Presentation Only
The sidebar renders only the items a role is authorized to see. This does **not** replace server-side RBAC. A direct deep-link to `/audit` by an employee is still blocked by the P5-T2 `AuthGuard` / server-side session check. Nav hiding is a UX affordance, not a security boundary.

### Layout Guard Preserved
The `(app)/layout.tsx` guard (`<AuthGuard>`) is the outermost wrapper. `AppShell` is rendered inside it. The guard was not touched — session context from P5-T2 is available to all shell components via `useSession`.

### Search / Notifications Are Presentational
The search input and notifications bell render as UI only. No event handlers on search input, no `/notifications` endpoint call. Account menu is fully real: session data (`user.name`, `user.email`, `user.role`) from P5-T2 `useSession`; sign-out calls `useLogout()`.

### P5-T1 Primitives / P5-T2 Session Consumed, Not Rebuilt
Shell tokens (`--color-primary-600`, `--color-ai-primary`, `--surface-sidebar`, transition variables) come from `globals.css` (P5-T1). Auth state comes from `useSession` and `useLogout` (P5-T2). Nothing duplicated.

---

## DEVIATIONS / RECONCILIATIONS

1. **`button.tsx` compose-refs regression**: The `TooltipTrigger asChild` pattern on disabled buttons caused a React compose-refs infinite render loop in the P5-T1 gallery. Fixed here by removing `asChild` and using inline styles on the `TooltipTrigger` default span. The fix is minimal and does not change the visible tooltip behavior.

2. **`use-toast.ts` exhaustive-deps**: The subscription `useEffect` had `[state]` in its dependency array, causing re-subscription on every toast state change. Fixed to `[]` (mount/unmount only). The listener is registered once; state updates flow through the shared listener array.

3. **`globals.css` pb-safe**: The `MobileTabBar` uses `sm:hidden` to hide above the sm breakpoint. The `pb-safe` utility is a one-line CSS addition — not a design token change — to correctly handle iOS notch safe areas.

4. **layout.tsx prototype replacement**: The prototype shell in `(app)/layout.tsx` from P5-T2 was a static, non-interactive, non-role-aware placeholder. It was fully replaced by `AppShell` delegation. All nav config, session data, and responsive logic now lives in the new components.

5. **9 stubs vs. 8**: The task spec referenced "8 (app) routes" for stubs, but the working tree had 9 new untracked route directories (analytics, approvals, audit, expenses, planner, policy, receipts, trips, users). The dashboard page pre-existed from P5-T2. All 9 stubs were created.

---

## VALIDATION EVIDENCE

- **Role nav (employee)**: Logged in as `employee@atlas.dev`; sidebar showed Dashboard, Travel Planner, Trips, Receipts, Expenses (5 items). Management and Administration sections absent.
- **Role nav (manager)**: Logged in as `manager@atlas.dev`; sidebar showed Dashboard, Travel Planner, Trips, Receipts, Expenses + Management section (Approvals, Analytics).
- **Role nav (finance_admin)**: Logged in as `finance@atlas.dev`; sidebar showed Dashboard + Management section (Approvals, Analytics) + Administration section (Policy, Audit, Users). Travel/Expense items absent.
- **Forbidden deep-link**: Navigating an employee session to `/audit` was blocked by the P5-T2 `AuthGuard` and redirected to `/login`.
- **Four responsive tiers**: Verified at 1400px (expanded), 1100px (collapsed icon-only with tooltips), 900px (hamburger drawer), 500px (bottom tab bar + full-screen assistant).
- **Active-state**: Clicking each nav item updated the `aria-current="page"` link and rendered the 3px primary left border indicator.
- **Sign-out**: Account menu sign-out called `useLogout`, cleared the session, and redirected to `/login`.
- **FAB / launcher / Ctrl-J**: All three surfaces opened and closed the (empty) assistant drawer correctly. Escape key closed it.
- **A11y**: Skip link present (`#main-content`). All nav links carry `aria-current`; collapsed sidebar links carry `aria-label`. Sidebar close button carries `aria-label="Close menu"`. FAB and assistant close button carry descriptive `aria-label`. `prefers-reduced-motion` honored via CSS transition variables.
- **tsc**: Clean (0 errors).
- **eslint**: Clean (0 errors, 0 warnings).
- **next build**: Succeeded (0 errors, all routes statically analyzed).
- **Console**: Zero errors in browser DevTools across all roles and breakpoints.

Recordings and screenshots from the role-nav / responsive verification session are stored locally (not in the repository). They are excluded by `.gitignore` / `frontend/.gitignore`.

---

## FENCE CONFIRMATION

- No screen bodies, dashboard widgets, or data-fetching logic in any stub page.
- No assistant chat UI, no `EventSource`, no `/messages` endpoint, no `MessageList` or `ChatBubble` component — the assistant slot renders a placeholder only.
- No search wiring, no `/search` endpoint, no notifications endpoint.
- `backend/`, `ai_agents/`, `database/` — zero diff.
- P5-T1 primitives and P5-T2 session consumed as dependencies; not rebuilt.

---

## KNOWN / CARRIED FORWARD

| Item | Deferral Target |
|---|---|
| Search field wiring (autocomplete, results) | Post-P5 / TBD |
| Notifications endpoint + real count badge | Post-P5 / TBD |
| Assistant conversational UI (chat, SSE) | P5-T10 |
| Placeholder stubs awaiting real screens | P5-T4 (dashboard), P5-T5..T9 (remaining) |

---

## WHAT'S NEXT

**P5-T4** — Dashboard + widgets wired to `GET /analytics/overview`.
