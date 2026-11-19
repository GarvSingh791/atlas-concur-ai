/**
 * Remembered organisation login code (known limits: organisation-aware login).
 *
 * The code is NOT a secret — finance admins share it with their users — so it is
 * kept in localStorage per device, purely as a convenience: the login page opens
 * the "Organisation" field pre-filled next time. Storage can be unavailable
 * (private mode, blocked site data); every access is guarded and failure just
 * means "nothing remembered".
 */

const ORG_KEY = "atlas_login_org";

export function getRememberedOrganization(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(ORG_KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberOrganization(code: string): void {
  if (typeof window === "undefined") return;
  try {
    const value = code.trim();
    if (value) window.localStorage.setItem(ORG_KEY, value);
    else window.localStorage.removeItem(ORG_KEY);
  } catch {
    // Storage unavailable — nothing to remember.
  }
}
