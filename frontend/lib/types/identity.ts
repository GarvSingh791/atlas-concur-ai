/**
 * Identity domain DTO mirrors.
 *
 * Exact mirrors of:
 *   backend/app/modules/identity/schemas.py
 *   database/models/identity.py  (RoleKey, UserStatus enums)
 *   config/constants/permissions.py  (permission key catalog)
 *   database/seeds/identity_seed.py  (role → permission mapping)
 *
 * NEVER hand-type strings — all role/permission values derive from
 * the backend source of truth above.
 */

// ---------------------------------------------------------------------------
// Enums (mirror database/models/identity.py)
// ---------------------------------------------------------------------------

/** Coarse user role — mirrors RoleKey str enum in identity.py. */
export type RoleKey = "employee" | "manager" | "finance_admin";

export const ROLE_KEYS = [
  "employee",
  "manager",
  "finance_admin",
] as const satisfies readonly RoleKey[];

/** User life-cycle status — mirrors UserStatus str enum. */
export type UserStatus = "active" | "inactive";

// ---------------------------------------------------------------------------
// Permission keys (mirror config/constants/permissions.py)
// ---------------------------------------------------------------------------

/**
 * All permission keys in the system.
 * Catalog: config/constants/permissions.py PERMISSION_CATALOG
 */
export type PermissionKey =
  // Expense
  | "expense.read.own"
  | "expense.create.own"
  | "expense.update.own"
  | "expense.read.reports"
  | "expense.read.org"
  | "expense.reimburse.org"
  // Approval
  | "approval.decide.reports"
  | "approval.read"
  // Policy
  | "policy.read"
  | "policy.manage"
  // User management
  | "users.read"
  // Trip
  | "trip.read.own"
  | "trip.create.own"
  | "trip.update.own";

/**
 * Static role → permission mapping.
 * Authoritative source: database/seeds/identity_seed.py _ROLE_PERMISSIONS.
 *
 * Permissions are resolved server-side from role_permissions DB table;
 * /me does not return permissions. This client-side map mirrors the seed
 * for UI decision-making (e.g. showing/hiding nav items). The server always
 * enforces — never trust this map for security decisions.
 */
export const ROLE_PERMISSIONS: Record<RoleKey, readonly PermissionKey[]> = {
  employee: [
    "expense.read.own",
    "expense.create.own",
    "expense.update.own",
    "policy.read",
    "trip.read.own",
    "trip.create.own",
    "trip.update.own",
  ],
  manager: [
    "expense.read.own",
    "expense.create.own",
    "expense.update.own",
    "expense.read.reports",
    "approval.decide.reports",
    "approval.read",
    "policy.read",
    "trip.read.own",
    "trip.create.own",
    "trip.update.own",
  ],
  finance_admin: [
    "expense.read.own",
    "expense.read.org",
    "approval.read",
    "approval.decide.reports",
    "users.read",
    "policy.read",
    "policy.manage",
    "trip.read.own",
    "expense.reimburse.org",
  ],
} as const;

// ---------------------------------------------------------------------------
// Auth DTOs (mirror identity/schemas.py)
// ---------------------------------------------------------------------------

/** POST /api/v1/auth/login request body. */
export interface LoginRequest {
  email: string;
  password: string;
  /** Optional organisation login code (organizations.slug); case-insensitive. */
  organization?: string;
}

/** GET /api/v1/organization (finance_admin) — the caller's own organisation. */
export interface OrganizationResponse {
  id: string;
  name: string;
  login_code: string;
}

/**
 * POST /api/v1/auth/login response (TokenResponse in schemas.py).
 * Both tokens delivered in body — backend sets no httpOnly cookie.
 */
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string; // "bearer"
}

/** POST /api/v1/auth/refresh request body. */
export interface RefreshRequest {
  refresh_token: string;
}

/**
 * POST /api/v1/auth/refresh response (AccessTokenResponse in schemas.py).
 * Refresh rotates only the access token; refresh token is unchanged.
 */
export interface AccessTokenResponse {
  access_token: string;
  token_type: string; // "bearer"
}

// ---------------------------------------------------------------------------
// User DTOs (mirror identity/schemas.py)
// ---------------------------------------------------------------------------

/**
 * GET /api/v1/me response (MeResponse in schemas.py).
 * Session context shape derives from this — not from a guess.
 */
export interface MeResponse {
  id: string; // UUID serialised as string
  name: string;
  email: string;
  role: RoleKey;
  department: string | null;
  organization_id: string; // UUID serialised as string
  /** Phase 12 follow-ups: null = email not verified (a banner, never a block).
   * Absent on older servers. */
  email_verified_at?: string | null;
  /** Phase 11 (DEMO_MODE): a shared demo account (no password change, no
   * verification email). Absent on older servers. */
  demo_protected?: boolean;
}

/**
 * GET /api/v1/users and GET /api/v1/users/{id} response (UserResponse in schemas.py).
 */
export interface UserResponse {
  id: string;
  name: string;
  email: string;
  role: RoleKey;
  department: string | null;
  manager_id: string | null;
  status: UserStatus;
  organization_id: string;
  created_at: string; // ISO 8601
  /** Traveller grade code (GET /grades ladder) — null = not recorded (AIR-04). */
  grade: string | null;
  /** Phase 11 (DEMO_MODE): a shared demo account nobody may edit or deactivate.
   * A UI hint only — the server enforces it. Absent on older servers. */
  demo_protected?: boolean;
  /** Phase 12 follow-ups (account recovery). Absent on older servers. */
  email_verified_at?: string | null;
  /** An invited account that has not accepted yet (it stays inactive). */
  invite_pending?: boolean;
  /** That invitation's expiry (ISO 8601); in the past = expired. */
  invite_expires_at?: string | null;
  /** Invited and never activated (open, expired or revoked invitation). */
  awaiting_activation?: boolean;
}

// ---------------------------------------------------------------------------
// Account recovery DTOs (phase 12 follow-ups — mirror identity/schemas.py)
// ---------------------------------------------------------------------------

/** 202 body of forgot-password / send-verification (same whatever happened). */
export interface AcceptedResponse {
  message: string;
}

export interface ForgotPasswordRequest {
  email: string;
  organization?: string;
}

export interface ResetPasswordRequest {
  token: string;
  new_password: string;
}

export interface AcceptInviteRequest {
  token: string;
  password: string;
  name?: string;
}

/** GET /auth/invite/{token} — what the accept page shows. */
export interface InvitePeekResponse {
  organization_name: string;
  email_masked: string;
  name: string;
  expires_at: string;
}
