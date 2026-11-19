/**
 * Product gap 2 — entry policy fields (attendees, gift disclosure, mileage).
 *
 * Pure helpers only. The server owns every decision: it gates each field by
 * category, computes a mileage amount (business miles × the MIL-01 rate) and
 * evaluates MEAL-05 / ENT-02 / ENT-04 / MIL-01..03. The one number computed
 * here (`estimateMileageAmount`) is a labelled, display-only estimate.
 */

import type {
  EntryDisclosure,
  ExpenseCategory,
  ExpenseEntry,
  MileageTripInput,
} from "@/lib/types";

/** Meals / client entertainment record attendees (MEAL-05 / ENT-02). */
export const ATTENDEE_CATEGORIES: ReadonlySet<ExpenseCategory> =
  new Set<ExpenseCategory>(["meals", "entertainment"]);
/** Gifts / client entertainment carry the ENT-04 disclosure. */
export const DISCLOSURE_CATEGORIES: ReadonlySet<ExpenseCategory> =
  new Set<ExpenseCategory>(["gift", "entertainment"]);
/** Only "Add mileage" creates a mileage entry; an entry never becomes one. */
export const MILEAGE_CATEGORY: ExpenseCategory = "mileage";

export const GIFT_DISCLOSURE_THRESHOLD_USD = 100; // ENT-04 (gifts > $100)
export const MAX_ATTENDEES = 500;
/** A distance: up to 5 digits, at most one decimal (0.1 mile / km). */
export const DISTANCE_RE = /^\d{1,5}(\.\d)?$/;
const MILES_PER_KM_DIVISOR = 1.609344;

export const showsAttendees = (c: ExpenseCategory): boolean =>
  ATTENDEE_CATEGORIES.has(c);
export const showsDisclosure = (c: ExpenseCategory): boolean =>
  DISCLOSURE_CATEGORIES.has(c);
export const isMileage = (c: ExpenseCategory): boolean =>
  c === MILEAGE_CATEGORY;

/** Form-side strings for the trip (what the inputs hold). */
export interface MileageForm {
  distance: string;
  unit: "mi" | "km";
  origin: string;
  destination: string;
  roundTrip: boolean;
  commuteOffset: string;
  airImpractical: boolean;
}

export const EMPTY_MILEAGE_FORM: MileageForm = {
  distance: "",
  unit: "mi",
  origin: "",
  destination: "",
  roundTrip: false,
  commuteOffset: "0",
  airImpractical: false,
};

export function mileageFormOf(entry: ExpenseEntry): MileageForm {
  const m = entry.mileage;
  if (!m) return { ...EMPTY_MILEAGE_FORM };
  return {
    distance: m.distance ?? "",
    unit: m.unit === "km" ? "km" : "mi",
    origin: m.origin ?? "",
    destination: m.destination ?? "",
    roundTrip: m.round_trip === true,
    commuteOffset: m.commute_offset ?? "0",
    airImpractical: m.air_impractical === true,
  };
}

/** The request shape (trimmed); never an amount or a rate. */
export function mileageInputOf(form: MileageForm): MileageTripInput {
  return {
    distance: form.distance.trim(),
    unit: form.unit,
    origin: form.origin.trim(),
    destination: form.destination.trim(),
    round_trip: form.roundTrip,
    commute_offset: form.commuteOffset.trim() || "0",
    air_impractical: form.airImpractical,
  };
}

/** A user-facing problem with the trip, or null when it can be sent. */
export function mileageFormError(form: MileageForm): string | null {
  const distance = form.distance.trim();
  if (!DISTANCE_RE.test(distance) || Number(distance) <= 0) {
    return "Enter the one-way distance, e.g. 42 or 42.5.";
  }
  const offset = form.commuteOffset.trim() || "0";
  if (!DISTANCE_RE.test(offset)) {
    return "Enter your normal commute distance to deduct (0 if none).";
  }
  if (!form.origin.trim() || !form.destination.trim()) {
    return "Enter where the trip started and ended.";
  }
  return null;
}

function tenths(value: string): number | null {
  if (!DISTANCE_RE.test(value)) return null;
  const [whole, frac = "0"] = value.split(".");
  return Number(whole) * 10 + Number(frac);
}

function milesTenths(value: string, unit: "mi" | "km"): number | null {
  const t = tenths(value);
  if (t === null) return null;
  return unit === "km" ? Math.round(t / MILES_PER_KM_DIVISOR) : t;
}

/**
 * DISPLAY-ONLY business miles of a trip ("221.0"), like the server: one-way
 * miles (x2 for a round trip) minus the commute offset (MIL-02). Null when it
 * cannot be estimated or nothing is left to reimburse.
 */
export function estimateBillableMiles(
  trip: Pick<
    MileageTripInput,
    "distance" | "unit" | "round_trip" | "commute_offset"
  >,
): string | null {
  const oneWay = milesTenths(trip.distance.trim(), trip.unit);
  const offset = milesTenths(trip.commute_offset.trim() || "0", trip.unit);
  if (oneWay === null || offset === null || oneWay <= 0) return null;
  const billable = (trip.round_trip ? oneWay * 2 : oneWay) - offset;
  if (billable <= 0) return null;
  return `${Math.floor(billable / 10)}.${billable % 10}`;
}

/**
 * DISPLAY-ONLY estimate of a trip's amount at `ratePerMile` (e.g. "0.70") —
 * integer arithmetic in tenths of a mile and thousandths of a dollar, rounded
 * half up to the cent like the server. Null when it cannot be estimated (the
 * server's figure is the only one ever stored).
 */
export function estimateMileageAmount(
  trip: Pick<
    MileageTripInput,
    "distance" | "unit" | "round_trip" | "commute_offset"
  >,
  ratePerMile: string | null | undefined,
): string | null {
  const rate = /^(\d{1,2})(?:\.(\d{1,3}))?$/.exec((ratePerMile ?? "").trim());
  if (!rate) return null;
  const rateThousandths =
    Number(rate[1]) * 1000 + Number((rate[2] ?? "").padEnd(3, "0"));
  const oneWay = milesTenths(trip.distance.trim(), trip.unit);
  const offset = milesTenths(trip.commute_offset.trim() || "0", trip.unit);
  if (oneWay === null || offset === null || oneWay <= 0) return null;
  const billable = (trip.round_trip ? oneWay * 2 : oneWay) - offset;
  if (billable <= 0 || rateThousandths <= 0) return null;
  const cents = Math.floor((billable * rateThousandths + 50) / 100);
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

/** Form-side disclosure ("" = not answered for the official question). */
export interface DisclosureForm {
  recipient: string;
  recipientOrganization: string;
  businessPurpose: string;
  governmentOfficial: "" | "yes" | "no";
}

export function disclosureFormOf(entry: ExpenseEntry): DisclosureForm {
  const d = entry.disclosure;
  return {
    recipient: d?.recipient ?? "",
    recipientOrganization: d?.recipient_organization ?? "",
    businessPurpose: d?.business_purpose ?? "",
    governmentOfficial:
      d?.government_official === true
        ? "yes"
        : d?.government_official === false
          ? "no"
          : "",
  };
}

/** The request shape; all-blank → null (no disclosure). */
export function disclosureOf(form: DisclosureForm): EntryDisclosure | null {
  const text = (v: string) => (v.trim() === "" ? null : v.trim());
  const record: EntryDisclosure = {
    recipient: text(form.recipient),
    recipient_organization: text(form.recipientOrganization),
    business_purpose: text(form.businessPurpose),
    government_official:
      form.governmentOfficial === "" ? null : form.governmentOfficial === "yes",
  };
  return Object.values(record).some((v) => v !== null) ? record : null;
}

export function sameDisclosure(
  a: EntryDisclosure | null | undefined,
  b: EntryDisclosure | null | undefined,
): boolean {
  const n = (d: EntryDisclosure | null | undefined) =>
    d
      ? [
          d.recipient ?? null,
          d.recipient_organization ?? null,
          d.business_purpose ?? null,
          d.government_official ?? null,
        ]
      : [null, null, null, null];
  return JSON.stringify(n(a)) === JSON.stringify(n(b));
}

/** "" → null; otherwise a whole number 1..500, or undefined when invalid. */
export function parseAttendees(value: string): number | null | undefined {
  const v = value.trim();
  if (v === "") return null;
  if (!/^\d{1,3}$/.test(v)) return undefined;
  const n = Number(v);
  return n >= 1 && n <= MAX_ATTENDEES ? n : undefined;
}
