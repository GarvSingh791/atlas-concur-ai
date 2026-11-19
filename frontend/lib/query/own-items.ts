/**
 * Belt-and-braces for the personal lists (UAT2 Issue 7 deploy window).
 *
 * The personal "Expenses" and "My Trips" lists send `mine=true` and rely on the
 * server to narrow the result to the caller's own records. During a deploy the
 * frontend can go live a few minutes before the backend that understands
 * `mine=true`; an older backend ignores the flag and returns the role's full
 * read scope (a manager would see their team's records on a personal page).
 *
 * This drops any item the caller does not own. Against a current backend it is a
 * no-op. With no session user yet (still hydrating) the page is returned as is:
 * the server narrowing is the real control, and owner-only actions stay gated by
 * isRecordOwner regardless.
 */
import { useAuthStore } from "@/stores/auth-store";
import type { PaginatedResponse } from "@/lib/types/api";

export function keepCallerItems<T>(
  page: PaginatedResponse<T>,
  ownerOf: (item: T) => string | null | undefined,
  callerId: string | null | undefined = useAuthStore.getState().user?.id,
): PaginatedResponse<T> {
  if (!callerId || !page?.items) return page;
  const own = page.items.filter((item) => ownerOf(item) === callerId);
  if (own.length === page.items.length) return page;
  return {
    ...page,
    items: own,
    total: Math.max(0, page.total - (page.items.length - own.length)),
  };
}
