"use client";

import * as React from "react";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { Input } from "@/components/ui/input";
import {
  useAddMileage,
  useMileageRate,
  type MileageRate,
} from "@/lib/query/mileage-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import { formatCurrency } from "@/lib/expenses/format";
import {
  EMPTY_MILEAGE_FORM,
  estimateBillableMiles,
  estimateMileageAmount,
  mileageFormError,
  mileageInputOf,
  type MileageForm,
} from "@/lib/expenses/policy-fields";
import { isApiError } from "@/lib/types/api";
import type { ExpenseReport } from "@/lib/types";
import { MileageFields } from "./entry-policy-fields";

/**
 * "Add mileage" — product gap 2 (MIL-01..03, HOW-06).
 *
 * The claimant enters the trip (date, from / to, one-way distance, round trip,
 * commute offset, air impractical). There is NO amount field: the server
 * computes business miles × the MIL-01 rate and the toast shows its figure.
 * Follow-up: a live, display-only estimate (business miles × the current rate
 * from GET /mileage-rate, with its clause) shows before the claimant confirms.
 * Mount only while open (owner + draft; the server enforces both).
 */
export function AddMileageDialog({
  report,
  onOpenChange,
}: {
  report: Pick<ExpenseReport, "id" | "version">;
  onOpenChange: (open: boolean) => void;
}) {
  const [trip, setTrip] = React.useState<MileageForm>({
    ...EMPTY_MILEAGE_FORM,
  });
  const [date, setDate] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const add = useAddMileage();
  const rate = useMileageRate();
  const { showToast } = useAtlasToast();

  const handleConfirm = () => {
    if (add.isPending) return;
    if (!date) {
      setError("Enter the date of the trip.");
      return;
    }
    const problem = mileageFormError(trip);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    add.mutate(
      {
        reportId: report.id,
        reportVersion: report.version,
        body: { ...mileageInputOf(trip), expense_date: date },
      },
      {
        onSuccess: (entry) => {
          const m = entry.mileage;
          showToast({
            variant: "success",
            title: "Mileage added",
            description: m
              ? `${formatCurrency(entry.amount, "USD")} = ${m.billable_miles} business mi × $${m.rate_per_mile}/mi (${m.rate_clause}).`
              : `${formatCurrency(entry.amount, "USD")} added.`,
          });
          onOpenChange(false);
        },
        onError: (err) => {
          showToast(expenseErrorToast(err, "Couldn’t add the mileage"));
          if (isApiError(err) && err.code === "CONFLICT") onOpenChange(false);
        },
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="Add mileage"
      description="Personal-vehicle business travel. Atlas computes the amount from the distance and the policy mileage rate — no receipt is needed."
      confirmLabel={add.isPending ? "Adding…" : "Add mileage"}
      confirmLoading={add.isPending}
      onConfirm={handleConfirm}
      contentClassName="sm:max-w-2xl"
    >
      <div className="mt-3 flex flex-col gap-3" data-testid="add-mileage-form">
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="mileage-date"
            className="atlas-text-body font-medium text-foreground"
          >
            Date
          </label>
          <Input
            id="mileage-date"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </div>
        <MileageFields idp="mileage-new" value={trip} onChange={setTrip} />
        <MileageEstimate trip={trip} rate={rate.data ?? null} />
        {error && (
          <p
            role="alert"
            className="atlas-text-caption text-[var(--color-error)]"
          >
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

/** Live, display-only estimate; the server's figure is the one stored. */
function MileageEstimate({
  trip,
  rate,
}: {
  trip: MileageForm;
  rate: MileageRate | null;
}) {
  if (!rate) return null;
  const input = mileageInputOf(trip);
  const miles = estimateBillableMiles(input);
  const amount = miles
    ? estimateMileageAmount(input, rate.rate_per_mile)
    : null;
  return (
    <p
      data-testid="mileage-estimate"
      aria-live="polite"
      className="atlas-text-caption rounded-atlas-md border border-border bg-muted px-3 py-2 text-muted-foreground"
    >
      {amount && miles ? (
        <>
          Estimate:{" "}
          <span className="font-mono font-medium text-foreground">
            {formatCurrency(amount, rate.currency)}
          </span>{" "}
          = {miles} business mi × ${rate.rate_per_mile}/mi (rate{" "}
          {rate.rate_clause}). Atlas computes the final amount when you add it.
        </>
      ) : (
        <>
          Current rate: ${rate.rate_per_mile}/mi ({rate.rate_clause}). Enter the
          distance to see an estimate.
        </>
      )}
    </p>
  );
}
