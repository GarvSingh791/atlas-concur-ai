/**
 * QA round — labels for the "Trip (optional)" select of the New report dialog.
 *
 * Four trips read "ORD → JFK (2026-11-17)" and could not be told apart. Each
 * option now carries the trip title (route, dates, purpose) and its status;
 * options that are still identical get their creation date, then a counter.
 */

import { humanizeStatus } from "@/components/atlas";
import { tripTitle } from "@/lib/trips/summary";
import type { TripListItem } from "@/lib/types/trips";

type TripOption = Pick<
  TripListItem,
  | "id"
  | "origin"
  | "primary_destination"
  | "start_date"
  | "end_date"
  | "purpose"
  | "status"
  | "created_at"
>;

const STATUS_LABEL: Record<string, string> = {
  pending_approval: "Pending approval",
};

function groupBy<T>(items: T[], key: (item: T) => string): Map<string, T[]> {
  const out = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    out.set(k, [...(out.get(k) ?? []), item]);
  }
  return out;
}

/** trip id -> a distinct, readable option label. */
export function tripOptionLabels(trips: TripOption[]): Map<string, string> {
  const base = (t: TripOption) =>
    t.status
      ? `${tripTitle(t)} · ${STATUS_LABEL[t.status] ?? humanizeStatus(t.status)}`
      : tripTitle(t);
  const labels = new Map<string, string>();
  for (const group of Array.from(groupBy(trips, base).values())) {
    if (group.length === 1) {
      labels.set(group[0].id, base(group[0]));
      continue;
    }
    const withCreated = (t: TripOption) =>
      `${base(t)} · created ${(t.created_at ?? "").slice(0, 10)}`;
    for (const same of Array.from(groupBy(group, withCreated).values())) {
      same.forEach((t, i) =>
        labels.set(
          t.id,
          same.length === 1 ? withCreated(t) : `${withCreated(t)} (${i + 1})`,
        ),
      );
    }
  }
  return labels;
}
