/**
 * Receipt image-link timing — C2 (NFR-SEC4).
 *
 * GET /receipts/{id} returns a short-lived signed `image_access_url` (TTL capped
 * at 300 s server-side), its absolute `image_access_expires_at` and its relative
 * lifetime `image_access_expires_in`. The receipt query refetches shortly
 * BEFORE expiry so a rendered <img> never points at a dead link.
 *
 * Scheduling is anchored on when the response ARRIVED (the query's
 * `dataUpdatedAt`, browser clock) plus the relative lifetime, so it never
 * compares the server's absolute timestamp against a possibly-skewed browser
 * clock. A fast client clock would otherwise make every fresh link look expired
 * and pin the refresh to MIN_REFRESH_MS forever. The absolute timestamp is only
 * a fallback for a response without `image_access_expires_in`, and even then
 * the deadline is clamped to the 300 s server cap measured from arrival.
 * Pure + clock-injectable so it is trivially unit-testable.
 */

/** Refresh this long before the link expires. */
export const REFRESH_LEAD_MS = 30_000;
/** Never schedule a refresh sooner than this (guards against refetch storms). */
export const MIN_REFRESH_MS = 5_000;
/** Server-side hard cap on a signed link's lifetime (NFR-SEC4). */
export const MAX_LINK_TTL_MS = 300_000;

export interface ImageLinkTiming {
  image_access_expires_at?: string | null;
  image_access_expires_in?: number | null;
}

/**
 * Milliseconds until the receipt query should refetch for a fresh link, or
 * `false` when there is no link expiry — i.e. nothing to refresh.
 *
 * @param link       the receipt payload (only the expiry fields are read)
 * @param receivedAt when that payload arrived (browser clock, ms epoch)
 * @param now        current browser time (ms epoch)
 */
export function msUntilRefresh(
  link: ImageLinkTiming | null | undefined,
  receivedAt: number,
  now: number = Date.now(),
): number | false {
  if (!link) return false;
  const maxDeadline = receivedAt + MAX_LINK_TTL_MS;
  let deadline: number;
  const expiresIn = link.image_access_expires_in;
  if (typeof expiresIn === "number" && Number.isFinite(expiresIn)) {
    deadline = Math.min(
      receivedAt + Math.max(0, expiresIn) * 1000,
      maxDeadline,
    );
  } else {
    if (!link.image_access_expires_at) return false;
    const expiry = Date.parse(link.image_access_expires_at);
    if (Number.isNaN(expiry)) return false;
    deadline = Math.min(expiry, maxDeadline);
  }
  return Math.max(MIN_REFRESH_MS, deadline - now - REFRESH_LEAD_MS);
}
