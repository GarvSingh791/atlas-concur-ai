/**
 * Trip display helpers (UAT-2 Issue 4).
 *
 * One planning conversation saves ONE draft trip with a flight and a hotel, so
 * the trip card needs a title that tells trips apart (route + dates + purpose)
 * and a line per saved leg with its price. Pure formatting — no business logic.
 */
import type { TripSegment } from "@/lib/types";

export interface TripHeader {
  origin: string;
  primary_destination: string;
  start_date: string;
  end_date: string;
  purpose?: string | null;
}

/** "ORD → JFK · 2026-11-17 – 2026-11-20 · Client meeting". */
export function tripTitle(trip: TripHeader): string {
  const when =
    !trip.end_date || trip.end_date === trip.start_date
      ? trip.start_date
      : `${trip.start_date} – ${trip.end_date}`;
  const parts = [`${trip.origin} → ${trip.primary_destination}`, when];
  const purpose = trip.purpose?.trim();
  if (purpose) parts.push(purpose);
  return parts.join(" · ");
}

/** "$1,234.50" for a finite amount (Decimal strings included), else null. */
export function formatUsd(
  amount: string | number | null | undefined,
): string | null {
  if (amount === null || amount === undefined || amount === "") return null;
  const n = typeof amount === "number" ? amount : Number(amount);
  if (!Number.isFinite(n)) return null;
  return `$${n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const SEGMENT_LABEL: Record<string, string> = {
  flight: "Flight",
  hotel: "Hotel",
  rail: "Rail",
  car: "Car",
};

function nightsOf(segment: TripSegment): number | null {
  const raw = segment.details?.nights;
  return typeof raw === "number" && Number.isFinite(raw) && raw >= 1
    ? raw
    : null;
}

/** "Flight · Delta Air Lines · $147.00" / "Hotel · Hilton · 3 nights · $654.00". */
export function segmentLine(segment: TripSegment): string {
  const parts = [SEGMENT_LABEL[segment.segment_type] ?? segment.segment_type];
  if (segment.supplier) parts.push(segment.supplier);
  const nights = segment.segment_type === "hotel" ? nightsOf(segment) : null;
  if (nights !== null) {
    parts.push(`${nights} ${nights === 1 ? "night" : "nights"}`);
  }
  const amount = formatUsd(segment.amount);
  if (amount) parts.push(amount);
  return parts.join(" · ");
}
