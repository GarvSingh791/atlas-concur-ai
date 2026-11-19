# P5-T2 — API Client, Types, Auth/Session (Phase 5)

- STATUS: complete, validated, uncommitted at time of writing.
- WHAT WAS BUILT:
    * Typed API client (frontend/lib): /api/v1 base from NEXT_PUBLIC_API_BASE_URL; bearer injection; success/error envelope unwrap; typed ApiError (message/code/correlation id); 401 -> single-flight refresh-once -> retry -> on refresh failure clear session + redirect; SSE streaming transport helper (UI consumer deferred to P5-T10).
    * DTO mirrors: shared (envelope, paginated meta, error, sort/filter) + identity (User, Role, Permission [from backend constants], AuthTokens, LoginRequest, MeResponse), mirrored from the REAL backend schemas. Domain DTOs left types-only.
    * Query layer: TanStack Query provider + defaults (no retry on 401); auth hooks (useLogin / useRefresh / useSession); documented resource-hook pattern for later screens.
    * Auth/session context: { user, role, permissions, status }, hydrated from GET /me; fail-closed boundary.
    * Protected-route boundary: (app) gate redirects unauthenticated -> /(auth)/login; role + permissions exposed in context; functional login form built from P5-T1 primitives.
- FILES CREATED / MODIFIED:
    * NEW: `frontend/lib/types/api.ts`, `frontend/lib/types/identity.ts`, `frontend/lib/types/index.ts`
    * NEW: `frontend/lib/auth/token-store.ts`, `frontend/lib/auth/session-provider.tsx`
    * NEW: `frontend/stores/auth-store.ts`
    * NEW: `frontend/lib/api/client.ts`, `frontend/lib/api/stream.ts`, `frontend/lib/api/index.ts`
    * NEW: `frontend/lib/query/query-client.ts`, `frontend/lib/query/auth-hooks.ts`, `frontend/lib/query/query-patterns.md`
    * NEW: `frontend/app/(app)/auth-guard.tsx`
    * MODIFIED: `frontend/app/(auth)/login/page.tsx`
    * MODIFIED: `frontend/app/(app)/layout.tsx`
    * MODIFIED: `frontend/app/layout.tsx`
    * MODIFIED: `frontend/app/page.tsx`
    * MODIFIED: `frontend/.env.local.example`
- KEY DECISIONS:
    * TOKEN STORAGE: Access token in memory; refresh token in localStorage. This acknowledges the XSS/persistence tradeoff; httpOnly-cookie hardening is flagged as a P6 follow-up.
    * Envelope/error mirrored byte-for-byte from backend core (not the doc's abstract description). Used real shapes: `{"error": {"code": "...", "message": "...", "correlation_id": "..."}}`.
    * Single-flight 401 refresh — concurrent 401s share one refresh; no retry loop.
    * Boundary fails closed — no session => unauthenticated, never renders protected content.
    * Primitives/tokens consumed from P5-T1, not rebuilt.
- SECURITY POSTURE (record plainly):
    * No token logged anywhere in committed code; no secret behind a NEXT_PUBLIC_ var; .env.local uncommitted (.env.example updated); Phase 3 seed credentials NOT hardcoded into the login form, fixtures, or any committed file; verification screenshots/recordings excluded from the repo.
- DEVIATIONS / RECONCILIATIONS: 
    * Reconciled NEXT_PUBLIC_API_BASE_URL (with /api/v1) as the single source of truth for the API client; deprecated NEXT_PUBLIC_API_URL.
    * Rebuilt backend docker image to correctly copy `ai_agents/`, `database/`, and `services/` into the context so the identity seed runs correctly.
- VALIDATION EVIDENCE: login (employee/manager/finance) stores token + role in context; unauthenticated redirect; 401 -> refresh -> retry and refresh-failure -> redirect; typed error envelope surfaced; fail-closed with backend unreachable; tsc / eslint / build clean. Verification media excluded from the repo.
- FENCE CONFIRMATION: no app shell / dashboard / domain screens / domain resource hooks / chat UI / backend touched — those are P5-T3...T10.
- KNOWN / CARRIED FORWARD: domain DTOs left types-only; SSE consumer for P5-T10; httpOnly-cookie hardening for P6.
- WHAT'S NEXT: P5-T3 — app shell (role-aware sidebar + top bar + assistant launcher, responsive collapse).
