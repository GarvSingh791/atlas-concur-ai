# Phase Log

**Phase:**       Phase 1 — Project Bootstrap
**Sub-Phase:**   P1-T3 — Frontend Skeleton (Next.js)
**Task ID:**     P1-T3
**Date:**        2026-05-31
**Author:**      Garv Singh Saini
**Status:**      ✔ Complete, Verified, Security Reviewed

---

## Objective

Initialize the Next.js 14+ App Router frontend with TypeScript strict mode inside the existing `frontend/` folder. Create the root layout, auth route group with `/login` placeholder, protected app route group with shell stub and `/dashboard` placeholder. Configure Tailwind CSS with Atlas design tokens and shadcn/ui as the component library. No real authentication, API calls, or business logic — a clean, type-safe, renderable frontend skeleton only.

---

## Work Completed

- Initialized Next.js 14.2.35 App Router with TypeScript strict mode inside `frontend/` (pinned `create-next-app@14` to guarantee Tailwind v3 + `tailwind.config.ts` token model; Next 15/16 would have introduced Tailwind v4 which drops this file)
- Configured Tailwind CSS v3 with full Atlas design token palette — accent blue `#2563EB`, calm background `#FAFAFA`, surface white `#FFFFFF`, border `#E5E7EB`, text-primary `#111827`, text-muted `#6B7280`, in-policy green `#16A34A`, out-policy / destructive red `#DC2626`, success `#16A34A`, warning `#D97706`; all expressed as HSL channel CSS custom properties in `styles/globals.css`
- Established single source-of-truth token system: every literal HSL value defined once in `styles/globals.css :root`; `tailwind.config.ts` references them via `hsl(var(--x))`; Atlas-named keys (`in-policy`, `out-policy`, `surface`, `success`, `warning`, `text-primary`, `text-muted`) alias the same vars as their shadcn semantic twins — no duplication
- Set up shadcn/ui (pinned `shadcn@2` for Tailwind v3 compatibility) with 13 base components: Button, Card, Badge, Input, Label, Separator, Skeleton, Toast, Toaster, Tooltip, Dialog, DropdownMenu, Avatar
- Created `lib/utils.ts` with `cn()` utility (clsx + tailwind-merge) — imported by all future components
- Configured `components.json` pointing `tailwind.css` to `styles/globals.css`, `ui` to `@/components/ui`, `utils` to `@/lib/utils`; CSS variables enabled, RSC mode enabled
- Created `(auth)` and `(app)` App Router route groups with correct layout separation — auth group has centered, no-sidebar layout; app group has persistent shell
- Built root layout (`app/layout.tsx`): Inter via `next/font/google` with `variable: "--font-sans"` (zero layout shift, no CDN dependency), imports `@/styles/globals.css`, mounts Toaster, exports metadata with Atlas title and description
- Root `app/page.tsx` redirects to `/login` (307) — no auth logic, clean stub
- Auth layout (`app/(auth)/layout.tsx`): full-viewport centered grid, subtle background, Atlas wordmark in accent blue
- Login page (`app/(auth)/login/page.tsx`): Atlas wordmark, `Sign in to Atlas` h1, subtitle, placeholder Card "Login form coming in Phase 2 — Authentication", non-functional primary Button "Continue"
- Protected app layout shell stub (`app/(app)/layout.tsx`): CSS Grid `grid-cols-[240px_1fr] grid-rows-[56px_1fr]`; sidebar `hidden md:flex` (hidden `<768px`, no JS required); sidebar contains Atlas wordmark, 8-item nav placeholder list with lucide-react icons (Dashboard, Planner, Trips, Receipts, Expenses, Approvals, Analytics, Policy), bottom user/account area with Avatar; top bar contains page title "Dashboard" and notification Bell + Avatar (initials fallback); layout is a server component
- Dashboard placeholder (`app/(app)/dashboard/page.tsx`): "Dashboard" h1, Card "Role-aware dashboard coming in Phase 5 — Frontend Development", three Skeleton stat cards in a responsive `md:grid-cols-3` grid
- Created `frontend/.env.local.example` with `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_NAME`, `NEXT_PUBLIC_APP_ENV` (example only — no real `.env.local` committed)
- Appended frontend env var documentation to root `.env.example` with note flagging the naming difference between `NEXT_PUBLIC_API_URL` (frontend skeleton) and the existing `NEXT_PUBLIC_API_BASE_URL` (root, includes `/api/v1` path) — to be reconciled by Phase 2 API client
- Replaced all Next.js default boilerplate: default splash content, Geist fonts, default SVGs (`public/next.svg`, `public/vercel.svg`), stale `app/globals.css` path
- Added `"type-check": "tsc --noEmit"` to `package.json` scripts; renamed package to `atlas-frontend`
- Added `*.tsbuildinfo` to root `.gitignore` to exclude TypeScript incremental build artifacts

---

## Files Created

```
frontend/package.json
frontend/package-lock.json
frontend/tsconfig.json
frontend/next.config.mjs
frontend/postcss.config.mjs
frontend/tailwind.config.ts
frontend/components.json
frontend/next-env.d.ts
frontend/.eslintrc.json
frontend/styles/globals.css
frontend/lib/utils.ts
frontend/lib/hooks/use-toast.ts
frontend/app/layout.tsx
frontend/app/page.tsx
frontend/app/(auth)/layout.tsx
frontend/app/(auth)/login/page.tsx
frontend/app/(app)/layout.tsx
frontend/app/(app)/dashboard/page.tsx
frontend/components/ui/button.tsx
frontend/components/ui/card.tsx
frontend/components/ui/badge.tsx
frontend/components/ui/input.tsx
frontend/components/ui/label.tsx
frontend/components/ui/separator.tsx
frontend/components/ui/skeleton.tsx
frontend/components/ui/toast.tsx
frontend/components/ui/toaster.tsx
frontend/components/ui/tooltip.tsx
frontend/components/ui/dialog.tsx
frontend/components/ui/dropdown-menu.tsx
frontend/components/ui/avatar.tsx
frontend/.env.local.example
docs/phase_logs/Phase1_P1-T3.md
```

---

## Files Modified

```
.env.example     — Appended frontend env var documentation block:
                   NEXT_PUBLIC_APP_NAME, NEXT_PUBLIC_APP_ENV; added
                   note flagging NEXT_PUBLIC_API_URL naming difference
                   vs existing NEXT_PUBLIC_API_BASE_URL
.gitignore       — Added *.tsbuildinfo to exclude TypeScript incremental
                   build artifacts from the repository
```

---

## Integration Points

- `frontend/app/(auth)/login/page.tsx` is the target shell for Phase 2 — authentication will wire the real login form, `POST /auth/login` API call, JWT token storage, and redirect to `/dashboard`
- `frontend/app/(app)/layout.tsx` shell stub is where Phase 5 adds real role-aware navigation (role-filtered nav items), the auth boundary check (redirect to `/login` if no valid session), and the reusable assistant launcher pinned at the sidebar bottom
- `frontend/lib/utils.ts` `cn()` utility is the single import for all component class merging — established once here, used by every future UI component
- Tailwind design tokens (`in-policy`, `out-policy`, `accent`, `surface`, `success`, `warning`) defined here are referenced throughout Phase 5 screens — policy badges, provenance chips, recommendation cards, status badges, and approval decision panels all consume these exact token names
- `frontend/.env.local.example` `NEXT_PUBLIC_API_URL` will point to the FastAPI backend established in P1-T2 — wired in Phase 2 when the first authenticated API calls begin. **Note:** the naming difference from root `NEXT_PUBLIC_API_BASE_URL` (which includes `/api/v1`) must be reconciled in Phase 2's API client layer
- shadcn/ui base components installed here are the foundation for Phase 5's screen-specific additions: Tables (expense list, approval queue), Charts (analytics widgets), Forms (receipt review, expense entry), DataTable (trips, reports)
- The `(app)` route group layout established here wraps every Phase 5 screen — `/planner`, `/trips`, `/receipts`, `/expenses`, `/approvals`, `/analytics`, `/policy` — without modification
- The `--font-sans` CSS variable wired here ensures Inter renders consistently across all future screens without re-configuration

---

## Technical Decisions

**App Router route groups `(auth)` vs `(app)`** — Parenthesized route groups apply separate layouts to URL segments without adding to the URL path. Auth screens (login, forgot-password) get a centered, no-sidebar layout; all authenticated screens share the persistent shell. The alternative — a single layout with conditional sidebar rendering based on a `pathname` check — would force the layout to be a client component and complicate future auth boundary logic. Route groups are the clean, idiomatic App Router approach.

**CSS custom properties alongside Tailwind config (dual token approach)** — All token values are defined once as HSL channel CSS variables in `styles/globals.css :root`, and `tailwind.config.ts` maps Tailwind utility names to `hsl(var(--x))`. This serves two needs: (1) Tailwind utility classes (`bg-primary`, `text-in-policy`) work with opacity modifiers (`bg-primary/50`) because HSL channel format enables the Tailwind opacity mechanism; (2) plain CSS and future CSS-in-JS can reference `hsl(var(--primary))` directly without going through Tailwind. Single source of truth, no duplication.

**Inter via `next/font/google` (not CDN)** — `next/font` downloads and self-hosts Inter at build time, inlines the `font-face` declaration, and generates an optimized CSS variable (`--font-sans`). This eliminates network round-trips to Google Fonts, prevents layout shift (FOUT), and removes a third-party resource dependency. CDN links require a `<link rel="preconnect">`, still incur layout shift, and expose user IPs to Google on page load.

**Server components as default, `"use client"` only at interactive leaves** — Next.js App Router server components have zero JS bundle cost and can import client components freely (they render to RSC payload on the server). The shell layout, all placeholder pages, and all layouts are server components. shadcn/ui components like Avatar, DropdownMenu, and Toaster are already marked `"use client"` in their own files and can be imported and rendered from server components. The only place a parent component would need `"use client"` is if it uses React hooks or event handlers directly — which none of these stubs do.

**shadcn/ui over pre-built component library** — shadcn/ui copies component source into the project (under `components/ui/`). This means: no runtime CSS-in-JS overhead, full Tailwind integration (classes are purged correctly, tokens are composable), complete ownership of each component's implementation, and ability to modify any component to meet Atlas's provenance badge or policy indicator requirements in Phase 5. Pre-built libraries (MUI, Ant Design) ship large runtime bundles and fight with custom design systems.

**CSS Grid for shell layout (not Flexbox or third-party)** — The shell has two independent axes: columns (sidebar width / content width) and rows (top bar height / content height). CSS Grid's two-dimensional model expresses this naturally in one declaration: `grid-cols-[240px_1fr] grid-rows-[56px_1fr]`. Flexbox requires nested containers. No third-party layout library was introduced per the task constraint.

**Pinned `shadcn@2` (not `shadcn@latest`)** — At the time of this task, `shadcn@latest` was v4-era and targets Tailwind v4, which replaces `tailwind.config.ts` with CSS `@theme` directives. The task spec explicitly requires `tailwind.config.ts` with custom tokens. `shadcn@2` is the last Tailwind v3 generation and writes the expected HSL CSS-variable token pattern — this was the most consequential version pin in the task.

---

## Testing Performed

All eleven Validate gate conditions from the task spec were satisfied:

- ✔ `npm run dev` starts without errors
- ✔ `http://localhost:3000` renders and 307-redirects to `/login`
- ✔ `/login` renders — Atlas wordmark, "Sign in to Atlas" h1, subtitle, placeholder Card, and non-functional "Continue" Button all visible
- ✔ `/dashboard` renders inside shell stub — sidebar area, 56px top bar area, and main content area structurally present
- ✔ `npm run type-check` passes with zero TypeScript errors (strict mode)
- ✔ `npm run lint` passes with zero ESLint errors
- ✔ No Next.js default boilerplate content visible anywhere
- ✔ Design tokens applied — `--background: 0 0% 98%` (#FAFAFA), `--primary: 217 91% 60%` (#2563EB), `--in-policy`, `--success` confirmed in compiled CSS
- ✔ shadcn/ui Button and Card render correctly with `border-radius: var(--radius)` = 0.5rem
- ✔ Shell layout structurally correct on desktop (240px sidebar via `md:grid-cols-[240px_1fr]`, 56px top bar via `grid-rows-[56px_1fr]`)
- ✔ Sidebar hidden on mobile viewport (`<768px`) via `hidden md:flex` — CSS-only, no JS

Additional QA performed:

- ✔ `npm run build` succeeded — production build clean; all 4 routes (`/`, `/_not-found`, `/dashboard`, `/login`) prerendered as static
- ✔ `/login` usable at 375px mobile viewport — centered card layout, no overflow
- ✔ `/dashboard` at 375px — sidebar hidden, top bar and content fully visible
- ✔ Browser console: zero errors and zero warnings on all pages
- ✔ Compiled CSS inspected — all Atlas custom properties (`--in-policy`, `--success`, `--accent-hover`, `--warning`, `--font-sans`) confirmed present; `background-color: hsl(var(--background))` applied to body
- ✔ No default Next.js content (black background, Vercel/Next SVG logos, "Create Next App" title) visible anywhere

---

## Risks Identified

- **`shadcn@latest` vs `shadcn@2` version conflict** — At the time of the build, `shadcn@latest` resolves to v4+ which targets Tailwind v4. Running it against this Tailwind v3 project would clobber the `tailwind.config.ts` token model. Mitigated by pinning `shadcn@2`.
- **NEXT_PUBLIC_API_URL naming mismatch** — `frontend/.env.local.example` defines `NEXT_PUBLIC_API_URL=http://localhost:8000` (per task spec), while root `.env.example` defines `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1`. These are different names and values. If not reconciled in Phase 2, the frontend API client would need to append `/api/v1` itself or read the wrong variable. Documented in both files and flagged for Phase 2.
- **`tsconfig.tsbuildinfo` untracked** — TypeScript incremental build artifact was generated in `frontend/` and not excluded by the original `.gitignore`. Mitigated by adding `*.tsbuildinfo` to `.gitignore` in this task.

---

## Resolution Actions

- `shadcn@latest` conflict → pinned `npx shadcn@2` for both `init` and `add` commands; verified `tailwind.config.ts` and `styles/globals.css` tokens were written in Tailwind v3 format
- `NEXT_PUBLIC_API_URL` mismatch → documented with explicit comment in both env files; noted in Integration Points for Phase 2 resolution
- `tsconfig.tsbuildinfo` → added `*.tsbuildinfo` pattern to root `.gitignore`

---

## Next Sub-Phase Preview

**P1-T4 — Datastore via Docker**

Objective: Add `docker-compose.yml` with PostgreSQL (pgvector-enabled), an S3-compatible object store (MinIO) for local dev, and backend and frontend services. Verify `docker compose up` brings up the database and object store, and the backend connects to PostgreSQL on boot.
