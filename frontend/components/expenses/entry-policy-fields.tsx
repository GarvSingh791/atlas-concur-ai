"use client";

import * as React from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { formatCurrency } from "@/lib/expenses/format";
import {
  GIFT_DISCLOSURE_THRESHOLD_USD,
  estimateMileageAmount,
  mileageInputOf,
  type DisclosureForm,
  type MileageForm,
} from "@/lib/expenses/policy-fields";
import type { ExpenseCategory, MileageTrip } from "@/lib/types";

/**
 * Product gap 2 — the entry fields policy needs, shown only for the
 * categories they belong to (the server enforces the same gating):
 *   - attendees: meals / client entertainment (MEAL-05 / ENT-02 per-head caps);
 *   - disclosure: gifts / client entertainment (ENT-04);
 *   - the mileage trip: mileage entries (MIL-01..03; amount computed server-side).
 */

export const POLICY_FIELD_CLASS =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Labelled({
  id,
  label,
  hint,
  className,
  children,
}: {
  id: string;
  label: string;
  hint?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <label
        htmlFor={id}
        className="atlas-text-body font-medium text-foreground"
      >
        {label}
      </label>
      {children}
      {hint && (
        <span className="atlas-text-caption text-muted-foreground">{hint}</span>
      )}
    </div>
  );
}

export function AttendeesField({
  idp,
  category,
  value,
  onChange,
}: {
  idp: string;
  category: ExpenseCategory;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Labelled
      id={`${idp}-attendees`}
      label="Attendees (including you)"
      hint={
        category === "meals"
          ? "Two or more makes this a team meal: it is checked against the per-head team dinner cap (MEAL-05) instead of your per diem, and your approver confirms the attendees — list them in the description."
          : "Client meals are capped per head by city tier (ENT-02) and your approver confirms the attendees (ENT-01) — list them in the description."
      }
    >
      <Input
        id={`${idp}-attendees`}
        data-testid="entry-attendees"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. 4"
      />
    </Labelled>
  );
}

export function DisclosureFields({
  idp,
  category,
  value,
  onChange,
}: {
  idp: string;
  category: ExpenseCategory;
  value: DisclosureForm;
  onChange: (v: DisclosureForm) => void;
}) {
  const set =
    <K extends keyof DisclosureForm>(key: K) =>
    (v: DisclosureForm[K]) =>
      onChange({ ...value, [key]: v });
  return (
    <fieldset
      data-testid="entry-disclosure"
      className="flex min-w-0 flex-col gap-3 rounded-atlas-md border border-border p-3 sm:col-span-2"
    >
      <legend className="atlas-text-body px-1 font-medium text-foreground">
        {category === "gift" ? "Gift disclosure" : "Hospitality disclosure"}
      </legend>
      <span className="atlas-text-caption text-muted-foreground">
        {category === "gift"
          ? `Required for gifts over $${GIFT_DISCLOSURE_THRESHOLD_USD} (ENT-04) — submission is blocked until it is complete.`
          : "Entertaining a government official needs Legal/Compliance pre-approval (ENT-04)."}
      </span>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Labelled id={`${idp}-recipient`} label="Recipient">
          <Input
            id={`${idp}-recipient`}
            value={value.recipient}
            onChange={(e) => set("recipient")(e.target.value)}
          />
        </Labelled>
        <Labelled id={`${idp}-recipient-org`} label="Recipient’s organisation">
          <Input
            id={`${idp}-recipient-org`}
            value={value.recipientOrganization}
            onChange={(e) => set("recipientOrganization")(e.target.value)}
          />
        </Labelled>
        <Labelled
          id={`${idp}-purpose`}
          label="Business purpose"
          className="sm:col-span-2"
        >
          <Input
            id={`${idp}-purpose`}
            value={value.businessPurpose}
            onChange={(e) => set("businessPurpose")(e.target.value)}
          />
        </Labelled>
        <Labelled
          id={`${idp}-official`}
          label="Is the recipient a government official?"
        >
          <select
            id={`${idp}-official`}
            value={value.governmentOfficial}
            onChange={(e) =>
              set("governmentOfficial")(
                e.target.value as DisclosureForm["governmentOfficial"],
              )
            }
            className={cn(POLICY_FIELD_CLASS, "h-10")}
          >
            <option value="">Not answered</option>
            <option value="no">No</option>
            <option value="yes">Yes</option>
          </select>
        </Labelled>
      </div>
    </fieldset>
  );
}

export function MileageFields({
  idp,
  value,
  onChange,
  stored,
}: {
  idp: string;
  value: MileageForm;
  onChange: (v: MileageForm) => void;
  /** The saved trip (server figures) — absent while adding a new one. */
  stored?: { trip: MileageTrip | null | undefined; amount: string } | null;
}) {
  const set =
    <K extends keyof MileageForm>(key: K) =>
    (v: MileageForm[K]) =>
      onChange({ ...value, [key]: v });
  const rate = stored?.trip?.rate_per_mile ?? null;
  const estimate = rate
    ? estimateMileageAmount(mileageInputOf(value), rate)
    : null;
  return (
    <fieldset
      data-testid="entry-mileage"
      className="flex min-w-0 flex-col gap-3 rounded-atlas-md border border-border p-3 sm:col-span-2"
    >
      <legend className="atlas-text-body px-1 font-medium text-foreground">
        Trip (personal vehicle)
      </legend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Labelled id={`${idp}-origin`} label="From">
          <Input
            id={`${idp}-origin`}
            value={value.origin}
            onChange={(e) => set("origin")(e.target.value)}
          />
        </Labelled>
        <Labelled id={`${idp}-destination`} label="To">
          <Input
            id={`${idp}-destination`}
            value={value.destination}
            onChange={(e) => set("destination")(e.target.value)}
          />
        </Labelled>
        <Labelled id={`${idp}-distance`} label="Distance, one way">
          <div className="flex gap-2">
            <Input
              id={`${idp}-distance`}
              data-testid="mileage-distance"
              inputMode="decimal"
              value={value.distance}
              onChange={(e) => set("distance")(e.target.value)}
              placeholder="e.g. 42.5"
            />
            <select
              aria-label="Distance unit"
              value={value.unit}
              onChange={(e) => set("unit")(e.target.value as "mi" | "km")}
              className={cn(POLICY_FIELD_CLASS, "h-10 w-24")}
            >
              <option value="mi">mi</option>
              <option value="km">km</option>
            </select>
          </div>
        </Labelled>
        <Labelled
          id={`${idp}-commute`}
          label="Normal commute to deduct"
          hint="MIL-02: business miles only — enter 0 if the trip replaced no commute."
        >
          <Input
            id={`${idp}-commute`}
            data-testid="mileage-commute"
            inputMode="decimal"
            value={value.commuteOffset}
            onChange={(e) => set("commuteOffset")(e.target.value)}
          />
        </Labelled>
        <label className="atlas-text-body flex items-center gap-2 text-foreground">
          <input
            type="checkbox"
            checked={value.roundTrip}
            onChange={(e) => set("roundTrip")(e.target.checked)}
          />
          Round trip
        </label>
        <label className="atlas-text-body flex items-center gap-2 text-foreground">
          <input
            type="checkbox"
            checked={value.airImpractical}
            onChange={(e) => set("airImpractical")(e.target.checked)}
          />
          Air travel was impractical (trips over 300 miles each way, MIL-03)
        </label>
      </div>
      <div
        data-testid="mileage-computed"
        className="atlas-text-caption flex flex-col gap-0.5 text-muted-foreground"
      >
        {stored?.trip ? (
          <>
            <span>
              Amount{" "}
              <span className="font-mono text-foreground">
                {formatCurrency(stored.amount, "USD")}
              </span>{" "}
              = {stored.trip.billable_miles} business mi × $
              {stored.trip.rate_per_mile}/mi (rate {stored.trip.rate_clause},
              computed by Atlas)
            </span>
            {estimate && (
              <span>
                With your changes: about {formatCurrency(estimate, "USD")} —
                Atlas recomputes it when you save.
              </span>
            )}
          </>
        ) : (
          <span>
            Atlas computes the amount: business miles × the mileage rate in
            policy MIL-01. You cannot enter it.
          </span>
        )}
      </div>
    </fieldset>
  );
}
