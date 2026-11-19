/**
 * Per-entry policy + match presentation for the approval detail (C7R).
 *
 * The approval's risk_summary.flags are the authoritative policy verdict for a
 * report under review (re-evaluated at approval time). Each flag points at the
 * entry it concerns through `entry_ref`, which the agents emit in a few shapes:
 *
 *   - the entry id itself, or `entry_<id>`            (id-based refs)
 *   - `segment_<trip_segment_id>` / `segment_id`      (trip-segment items)
 *   - the entry's vendor                              (presubmit `_entry_ref`)
 *   - `entry_<idx>` when the vendor is null — idx is the entry's position in the
 *     report ordered by (created_at, id), the order the agent evaluates in.
 *
 * This module only RE-ATTRIBUTES server flags to rows; it never evaluates
 * policy itself. A flag that matches no row still shows in the Exceptions list.
 *
 * Ambiguity: a vendor ref (presubmit `_entry_ref`) cannot tell two lines from
 * the same vendor apart. A BLOCKING flag that matches more than one row is
 * therefore NOT pinned on every one of them (that would mark a compliant line
 * "Out of policy"); each candidate row gets the "ambiguous" verdict instead —
 * rendered "Check exceptions", never a false "In policy" either. Id-based refs
 * (entry_id / entry id / segment id) are unique and always attribute exactly;
 * a flag with an `entry_id` is matched on it alone (no vendor fallback).
 */

import type { ExpenseEntry } from "@/lib/types/expenses";
import type { RiskFlag } from "@/lib/types/approvals";
import {
  isApproverException,
  isInternalEntryRef,
} from "@/lib/approvals/risk-summary";
import { CATEGORY_LABELS } from "@/lib/expenses/format";

export type EntryPolicyVerdict = "blocking" | "ambiguous" | "advisory" | "none";

const VERDICT_RANK: Record<EntryPolicyVerdict, number> = {
  none: 0,
  advisory: 1,
  ambiguous: 2,
  blocking: 3,
};

/** Flag fields the matcher reads; `entry_id` is set on every persisted entry's flag (C7R). */
type MatchableFlag = RiskFlag & { entry_id?: string | null };

function norm(v: string | null | undefined): string {
  return (v ?? "").trim().toLowerCase();
}

/** Epoch ms for an ISO timestamp, or NaN. Comparing the strings is wrong: the
 *  API omits the fraction when microseconds are 0 ("…:00Z" vs "…:00.25Z"), and
 *  "." sorts before "Z", inverting same-second order. */
function instant(iso: string): number {
  return Date.parse(iso);
}

/** Entry id → position in the backend's (created_at, id) evaluation order. */
function evaluationOrder(entries: ExpenseEntry[]): Map<string, number> {
  const sorted = [...entries].sort((a, b) => {
    const ta = instant(a.created_at);
    const tb = instant(b.created_at);
    if (!Number.isNaN(ta) && !Number.isNaN(tb)) {
      if (ta !== tb) return ta - tb;
      // Same millisecond: fall back to the sub-ms digits the string carries.
      const fa = subMillis(a.created_at);
      const fb = subMillis(b.created_at);
      if (fa !== fb) return fa - fb;
    } else if (a.created_at !== b.created_at) {
      return a.created_at < b.created_at ? -1 : 1;
    }
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return new Map(sorted.map((e, i) => [e.id, i]));
}

/** Microsecond digits beyond the millisecond (Date.parse drops them): 0–999. */
function subMillis(iso: string): number {
  const m = /\.(\d+)/.exec(iso);
  if (!m) return 0;
  const digits = m[1].padEnd(6, "0").slice(3, 6);
  return Number(digits);
}

function flagMatchesEntry(
  flag: MatchableFlag,
  entry: ExpenseEntry,
  index: number | undefined,
): boolean {
  const ref = norm(flag.entry_ref);
  const id = norm(entry.id);
  // C7R: a flag that carries entry_id (every persisted entry's flag does) is
  // attributed by id ONLY. Its entry_ref is then just a readable label (often
  // the vendor), and letting it fall through to the vendor match would pin the
  // flag on every same-vendor line too.
  const flagEntryId = norm(flag.entry_id);
  if (flagEntryId) return flagEntryId === id;
  if (entry.trip_segment_id) {
    const seg = norm(entry.trip_segment_id);
    if (norm(flag.segment_id) === seg) return true;
    if (ref === `segment_${seg}`) return true;
  }
  if (!ref) return false;
  if (ref === id || ref === `entry_${id}`) return true;
  if (entry.vendor && ref === norm(entry.vendor)) return true;
  if (!entry.vendor && index !== undefined && ref === `entry_${index}`) {
    return true;
  }
  return false;
}

/**
 * C7R UAT: a policy_violation (including one the claimant justified, which is
 * "advisory" for submit only) is an exception for the approver — the same rule
 * the server's blocking_flag_count and the Exceptions list use.
 */
function isBlocking(flag: RiskFlag): boolean {
  return norm(flag.severity) === "blocking" || isApproverException(flag);
}

/**
 * Map each entry id → the strongest policy verdict among the flags attributed
 * to it. Trip-style flags with `in_policy: true` are informational, not
 * exceptions, and are ignored.
 */
export function entryPolicyVerdicts(
  entries: ExpenseEntry[],
  flags: RiskFlag[] | null | undefined,
): Map<string, EntryPolicyVerdict> {
  const out = new Map<string, EntryPolicyVerdict>(
    entries.map((e) => [e.id, "none" as EntryPolicyVerdict]),
  );
  const order = evaluationOrder(entries);
  const relevant = (flags ?? []).filter(
    (f) => f && typeof f === "object" && f.in_policy !== true,
  );
  for (const flag of relevant) {
    const matched = entries.filter((e) =>
      flagMatchesEntry(flag, e, order.get(e.id)),
    );
    if (matched.length === 0) continue;
    const severe = isBlocking(flag) || flag.in_policy === false;
    const verdict: EntryPolicyVerdict = !severe
      ? "advisory"
      : matched.length === 1
        ? "blocking"
        : "ambiguous";
    for (const e of matched) {
      const prev = out.get(e.id) ?? "none";
      if (VERDICT_RANK[verdict] > VERDICT_RANK[prev]) out.set(e.id, verdict);
    }
  }
  return out;
}

/** Readable name for one entry: vendor, else description, else "<Category> entry". */
export function entryDisplayName(entry: ExpenseEntry): string {
  const vendor = entry.vendor?.trim();
  if (vendor) return vendor;
  const description = entry.description?.trim();
  if (description) return description;
  const category = CATEGORY_LABELS[entry.category] ?? "Expense";
  return `${category} entry`;
}

/**
 * QA round: the readable title for a flag whose entry_ref is an internal key
 * (`entry_<n>` for a line without a vendor, e.g. mileage). Resolves the flag to
 * its entry the same way the Policy column does (entry_id first, then the
 * evaluation-order index); null when it is not exactly one entry.
 */
export function flagEntryLabel(
  flag: RiskFlag,
  entries: ExpenseEntry[],
): string | null {
  if (!isInternalEntryRef(flag.entry_ref) && !flag.entry_id) return null;
  const order = evaluationOrder(entries);
  const matched = entries.filter((e) =>
    flagMatchesEntry(flag, e, order.get(e.id)),
  );
  return matched.length === 1 ? entryDisplayName(matched[0]) : null;
}
