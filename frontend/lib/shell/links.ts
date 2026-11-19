/**
 * Resolve an in-app link for the current session (product gap 3).
 *
 * The server returns a canonical `link` (e.g. "/expenses?report=…") and, where
 * one exists, a `fallback_link` (the subject's approval view). A role that
 * cannot open the canonical page — finance has no /expenses nav entry — gets
 * the fallback instead; when neither page is reachable the result is null and
 * the UI renders the item as non-navigable. Uses the SAME route gate as the
 * nav (canAccessPath), so search/notifications never link to a no-access page.
 */

import { canAccessPath } from "@/lib/nav-config";
import type { PermissionKey, RoleKey } from "@/lib/types/identity";

function pathOf(link: string): string {
  const q = link.indexOf("?");
  return q === -1 ? link : link.slice(0, q);
}

export function resolveLink(
  link: string | null | undefined,
  fallback: string | null | undefined,
  permissions: readonly PermissionKey[],
  role: RoleKey | null,
): string | null {
  for (const candidate of [link, fallback]) {
    // Internal routes only — never follow an absolute / protocol-relative URL.
    if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//"))
      continue;
    if (canAccessPath(pathOf(candidate), permissions, role)) return candidate;
  }
  return null;
}
