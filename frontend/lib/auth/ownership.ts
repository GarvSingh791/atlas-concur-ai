/**
 * Record ownership — the ONE predicate every owner-only control keys on (UAT2).
 *
 * A manager (team read scope) or finance admin (org-wide read scope) can READ
 * records they do not own, but only the owner may submit, resubmit, edit,
 * justify, move, rename, delete, attach receipts to or cancel them — the server
 * enforces this (403) and the UI must never offer those actions to a reader.
 *
 * Fails closed: no session user (still hydrating) or no owner id → not the
 * owner, so owner actions stay hidden.
 */
export function isRecordOwner(
  user: { id: string } | null | undefined,
  ownerId: string | null | undefined,
): boolean {
  return Boolean(user?.id) && Boolean(ownerId) && user?.id === ownerId;
}

/** True only when the viewer is KNOWN to be someone other than the owner. */
export function isKnownNonOwner(
  user: { id: string } | null | undefined,
  ownerId: string | null | undefined,
): boolean {
  return Boolean(user?.id) && user?.id !== ownerId;
}
