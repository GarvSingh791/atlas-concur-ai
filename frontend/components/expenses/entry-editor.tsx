"use client";

import * as React from "react";
import { Eye, Paperclip, Save, Trash2, X } from "lucide-react";

import { AtlasButton, Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { entryReceipts } from "./entry-receipts";
import { ReceiptViewerDialog } from "./receipt-viewer-dialog";
import { AmountConversionNote } from "@/components/atlas/amount-conversion-note";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useUpdateEntry } from "@/lib/query/expense-hooks";
import { useDeleteReceipt } from "@/lib/query/receipt-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import {
  CATEGORY_LABELS,
  formatCurrency,
  formatDate,
} from "@/lib/expenses/format";
import {
  CONFIRM_GROUND_TYPE_HINT,
  type ReadinessFlag,
} from "@/lib/expenses/readiness";
import {
  disclosureFormOf,
  disclosureOf,
  isMileage,
  mileageFormError,
  mileageFormOf,
  mileageInputOf,
  parseAttendees,
  sameDisclosure,
  showsAttendees,
  showsDisclosure,
  type DisclosureForm,
  type MileageForm,
} from "@/lib/expenses/policy-fields";
import {
  AttendeesField,
  DisclosureFields,
  MileageFields,
} from "./entry-policy-fields";
import { isApiError } from "@/lib/types/api";
import { displayText } from "@/lib/receipts/display-name";
import type {
  EntryReceipt,
  EntryReceiptRemoval,
  ExpenseCategory,
  ExpenseEntry,
  ExpenseEntryUpdate,
  ExpenseReport,
} from "@/lib/types";

/**
 * Inline entry editor — C7R UAT.
 *
 * Shown in the report detail for a DRAFT report the viewer owns (the backend
 * enforces both). Edits amount / vendor / category / date / description and the
 * policy-exception justification through PATCH /expenses/{id}, sending only the
 * fields that changed and the report version as `If-Match`. The server
 * re-derives the total and re-runs the pre-submit check; this form never
 * evaluates policy — it only shows the flags the server attributed to the entry.
 */

// Product gap 2: an entry never becomes a mileage entry ("Add mileage" creates
// those, with a server-computed amount) and a mileage entry keeps its category.
const CATEGORIES = (Object.keys(CATEGORY_LABELS) as ExpenseCategory[]).filter(
  (c) => !isMileage(c),
);
const AMOUNT_RE = /^\d{1,12}(\.\d{1,2})?$/;
const JUSTIFICATION_MAX = 2000;

const FIELD_CLASS =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

interface FormState {
  amount: string;
  vendor: string;
  category: ExpenseCategory;
  expenseDate: string;
  description: string;
  justification: string;
  /** EXP-04: the owner signs the missing-receipt affidavit (server stamps when). */
  affidavit: boolean;
  /** Product gap 2: attendees / disclosure / mileage trip (category-gated). */
  attendees?: string;
  disclosure?: DisclosureForm;
  mileage?: MileageForm;
}

/** The EXP-04 clause, shown verbatim next to the affidavit checkbox. */
export const AFFIDAVIT_CLAUSE =
  "EXP-04 Lost receipts: a signed missing-receipt affidavit is allowed up to $75 and no more than 3 times per quarter.";

function initialState(entry: ExpenseEntry): FormState {
  return {
    amount: String(entry.amount ?? ""),
    vendor: entry.vendor ?? "",
    category: entry.category,
    expenseDate: (entry.expense_date ?? "").slice(0, 10),
    description: entry.description ?? "",
    justification: entry.justification ?? "",
    affidavit: Boolean(entry.receipt_affidavit_at),
    attendees:
      entry.attendee_count === null || entry.attendee_count === undefined
        ? ""
        : String(entry.attendee_count),
    disclosure: disclosureFormOf(entry),
    mileage: mileageFormOf(entry),
  };
}

/**
 * The claim an EXP-04 affidavit vouches for (server AFFIDAVIT_EVIDENCE_FIELDS:
 * amount, expense_date, vendor, description; currency is not editable here).
 * Editing any of them withdraws the signature.
 */
const AFFIDAVIT_EVIDENCE: readonly (keyof ExpenseEntryUpdate)[] = [
  "amount",
  "expense_date",
  "vendor",
  "description",
];
const AFFIDAVIT_EVIDENCE_FORM: readonly (keyof FormState)[] = [
  "amount",
  "expenseDate",
  "vendor",
  "description",
];

/**
 * Only the fields the user actually changed (blank text → null clears it).
 *
 * `recomputeMileage` (product gap 2): the server blocks this mileage line (e.g.
 * finance changed the MIL-01 rate, so the stored amount is stale) — send the
 * trip even when it is unchanged so the server recomputes the amount.
 */
export function diffEntryChanges(
  entry: ExpenseEntry,
  form: FormState,
  { recomputeMileage = false }: { recomputeMileage?: boolean } = {},
): ExpenseEntryUpdate {
  const changes: ExpenseEntryUpdate = {};
  const text = (v: string) => (v.trim() === "" ? null : v.trim());
  // A mileage amount is computed by the server — never sent (MIL-01).
  if (
    !isMileage(entry.category) &&
    Number(form.amount) !== Number(entry.amount)
  )
    changes.amount = form.amount;
  if (form.category !== entry.category) changes.category = form.category;
  if (form.expenseDate !== (entry.expense_date ?? "").slice(0, 10)) {
    changes.expense_date = form.expenseDate;
  }
  if (text(form.vendor) !== (entry.vendor ?? null)) {
    changes.vendor = text(form.vendor);
  }
  if (text(form.description) !== (entry.description ?? null)) {
    changes.description = text(form.description);
  }
  if (text(form.justification) !== (entry.justification ?? null)) {
    changes.justification = text(form.justification);
  }
  if (form.affidavit !== Boolean(entry.receipt_affidavit_at)) {
    changes.receipt_affidavit = form.affidavit;
  } else if (
    form.affidavit &&
    AFFIDAVIT_EVIDENCE.some((key) => key in changes)
  ) {
    // QA round: the server withdraws a signature when the claim changes, so a
    // box ticked again after an edit re-signs explicitly for the edited claim.
    changes.receipt_affidavit = true;
  }
  // Product gap 2 — only the fields the (new) category shows; the server clears
  // the others when the category changes.
  if (form.attendees !== undefined && showsAttendees(form.category)) {
    const count = parseAttendees(form.attendees);
    if (count !== undefined && count !== (entry.attendee_count ?? null)) {
      changes.attendee_count = count;
    }
  }
  if (form.disclosure && showsDisclosure(form.category)) {
    const disclosure = disclosureOf(form.disclosure);
    if (!sameDisclosure(disclosure, entry.disclosure)) {
      changes.disclosure = disclosure;
    }
  }
  if (form.mileage && isMileage(entry.category)) {
    const trip = mileageInputOf(form.mileage);
    const was = entry.mileage;
    const changed =
      !was ||
      Number(trip.distance) !== Number(was.distance) ||
      trip.unit !== was.unit ||
      trip.origin !== was.origin ||
      trip.destination !== was.destination ||
      trip.round_trip !== was.round_trip ||
      Number(trip.commute_offset) !== Number(was.commute_offset) ||
      trip.air_impractical !== was.air_impractical;
    if (changed || recomputeMileage) changes.mileage = trip;
  }
  return changes;
}

function Field({
  id,
  label,
  children,
  hint,
  className,
}: {
  id: string;
  label: string;
  children: React.ReactNode;
  hint?: React.ReactNode;
  className?: string;
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

/**
 * UAT2 retest B: may this receipt be removed from the entry now? The server
 * says per receipt (`receipt_removal`, the same rule as DELETE /receipts — a
 * receipt attached after a return was never reviewed and may go). An older
 * backend without it keeps the report-level rule: none once reviewed.
 */
export function receiptRemovalState(
  entry: ExpenseEntry,
  report: Partial<Pick<ExpenseReport, "approval_status">>,
  receiptId: string,
): Omit<EntryReceiptRemoval, "receipt_id"> {
  if (entry.receipt_removal === undefined) {
    const reviewed = Boolean(report.approval_status);
    return {
      removable: !reviewed,
      kept_for_audit: reviewed,
      removal_blocked_reason: null,
    };
  }
  const state = entry.receipt_removal.find((r) => r.receipt_id === receiptId);
  return state
    ? state
    : { removable: false, kept_for_audit: false, removal_blocked_reason: null };
}

/**
 * UAT2 defect H: "Corner Deli · Jun 10, 2026 · $60.00" — what one receipt reads,
 * so receipts on one line can be told apart. Built from the server's per-receipt
 * list (`entry.receipts`, UAT2 defect F: the machine reading — never a typed or
 * AI-guessed value), the same data the approver sees. Only the parts read are
 * shown; null when nothing was read.
 */
export function receiptSummary(
  receipt: EntryReceipt,
  fallbackCurrency: string,
): string | null {
  const parts: string[] = [];
  // Display only: OCR'd vendor text may carry bidi / control characters.
  const vendor = displayText(receipt.vendor, 80);
  if (vendor) parts.push(vendor);
  if (receipt.receipt_date) parts.push(formatDate(receipt.receipt_date));
  if (receipt.amount !== null && receipt.amount !== "") {
    parts.push(
      formatCurrency(receipt.amount, receipt.currency ?? fallbackCurrency),
    );
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

export interface EntryEditorProps {
  entry: ExpenseEntry;
  report: Pick<ExpenseReport, "id" | "version"> &
    Partial<Pick<ExpenseReport, "approval_status">>;
  /** UAT2: open the attach-receipt dialog for this entry. */
  onAttachReceipt?: () => void;
  /** Server flags attributed to this entry (by entry id). */
  flags?: ReadinessFlag[];
  /** Focus the justification box on open (e.g. "Add justification"). */
  focusJustification?: boolean;
  onClose: () => void;
}

export function EntryEditor({
  entry,
  report,
  flags = [],
  focusJustification = false,
  onClose,
  onAttachReceipt,
}: EntryEditorProps) {
  // Optimistic concurrency: the entry + report version the user STARTED editing
  // from. A refetch while the form is open (e.g. another tab's edit) must not
  // silently advance the If-Match version — the save then 409s instead of
  // overwriting a change the user never saw.
  const [baseline] = React.useState(() => ({
    entry,
    version: report.version,
  }));
  const [form, setForm] = React.useState<FormState>(() => initialState(entry));
  const [error, setError] = React.useState<string | null>(null);
  const update = useUpdateEntry();
  const removeReceipt = useDeleteReceipt();
  const { showToast } = useAtlasToast();
  const justificationRef = React.useRef<HTMLTextAreaElement>(null);
  const idp = `entry-${entry.id}`;

  React.useEffect(() => {
    if (focusJustification) justificationRef.current?.focus();
  }, [focusJustification]);

  // QA round: the affidavit unticks the moment the claim it vouches for
  // changes (it used to stay ticked until the server cleared it on save).
  const [affidavitCleared, setAffidavitCleared] = React.useState(false);
  const set =
    <K extends keyof FormState>(key: K) =>
    (value: FormState[K]) =>
      setForm((prev) => {
        const next = { ...prev, [key]: value };
        if (key === "affidavit") {
          setAffidavitCleared(false);
        } else if (
          prev.affidavit &&
          AFFIDAVIT_EVIDENCE_FORM.includes(key) &&
          prev[key] !== value
        ) {
          next.affidavit = false;
          setAffidavitCleared(true);
        }
        return next;
      });

  const dirty =
    Object.keys(
      diffEntryChanges(baseline.entry, { ...form, amount: form.amount.trim() }),
    ).length > 0;
  // F's per-receipt list (machine reading, after-return verdict); an older
  // backend without it degrades to the bare ids (`detailed` false).
  const { receipts: receiptRows, detailed } = entryReceipts(entry);
  const receiptIds = receiptRows.map((r) => r.receipt_id);
  const affidavitOffered =
    receiptIds.length === 0 &&
    flags.some(
      (f) => f.flagType === "missing_receipt" && f.affidavitAllowed === true,
    );

  // Remove is offered per receipt, exactly where the server allows it: a
  // receipt a reviewer has seen is kept for audit; one attached after a return
  // can still go (UAT2 retest B).
  const receiptStates = receiptIds.map((rid) =>
    receiptRemovalState(entry, report, rid),
  );
  const anyKept = receiptStates.some((s) => s.kept_for_audit);
  const reviewedReport = Boolean(report.approval_status);
  // Not yet reviewed: the server says it arrived after the last return, or —
  // on a report that has been through review — it may still be removed (only
  // a receipt no reviewer has seen can be, UAT2 retest B).
  const notReviewed = receiptRows.map(
    (r, i) =>
      r.attached_after_return || (reviewedReport && receiptStates[i].removable),
  );
  const receiptLabels = receiptIds.map((_, i) =>
    receiptIds.length > 1 ? `Receipt ${i + 1}` : "Receipt",
  );
  const receiptSummaries = receiptRows.map((r) =>
    detailed ? receiptSummary(r, entry.currency) : null,
  );
  // The receipt open in the viewer (UAT2 defect H).
  // Keyed by receipt id (not list position) so a refetch can't swap the open receipt.
  const [viewingId, setViewingId] = React.useState<string | null>(null);
  const viewing = viewingId === null ? -1 : receiptIds.indexOf(viewingId);
  // The receipt awaiting the in-page "Remove this receipt?" confirm.
  const [confirmRemove, setConfirmRemove] = React.useState<{
    id: string;
    label: string;
  } | null>(null);

  const handleRemoveReceipt = (receiptId: string) => {
    setConfirmRemove(null);
    if (removeReceipt.isPending) return;
    removeReceipt.mutate(
      // From THIS line only: a receipt split across lines keeps backing the
      // others (UAT2 hardening); otherwise the receipt is deleted.
      { receiptId, fromEntryId: entry.id },
      {
        onSuccess: () => {
          showToast({
            variant: "success",
            title: "Receipt removed",
            description: "The pre-submit check has been re-run.",
          });
          // The report version moved on — reopen from the refetched entry.
          onClose();
        },
        onError: (err) =>
          showToast(expenseErrorToast(err, "Couldn’t remove the receipt")),
      },
    );
  };

  const exceptions = flags.filter((f) => f.flagType === "policy_violation");
  // Known limits (ground): the server could not confirm the ride's kind — say how
  // to confirm it here, at entry time, not only at approval.
  const groundTypeFlag = flags.find((f) => f.confirmGroundType === true);
  const justifiable = exceptions.some((f) => f.justifiable !== false);

  const mileageEntry = isMileage(entry.category);
  // A blocking mileage flag (stale rate / changed trip rules): Save re-sends the
  // trip so the server recomputes the line, even with nothing edited.
  const recomputeMileage =
    mileageEntry &&
    flags.some(
      (f) => f.flagType === "policy_violation" && f.ruleType === "mileage",
    );

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    if (!mileageEntry && !AMOUNT_RE.test(form.amount.trim())) {
      setError("Enter an amount like 245.00 (up to 2 decimals).");
      return;
    }
    if (
      showsAttendees(form.category) &&
      parseAttendees(form.attendees ?? "") === undefined
    ) {
      setError("Enter the number of attendees as a whole number (1–500).");
      return;
    }
    const tripError =
      mileageEntry && form.mileage ? mileageFormError(form.mileage) : null;
    if (tripError) {
      setError(tripError);
      return;
    }
    if (!form.expenseDate) {
      setError("Enter the expense date.");
      return;
    }
    setError(null);
    const changes = diffEntryChanges(
      baseline.entry,
      { ...form, amount: form.amount.trim() },
      { recomputeMileage },
    );
    if (Object.keys(changes).length === 0) {
      onClose();
      return;
    }
    update.mutate(
      {
        entryId: entry.id,
        reportId: report.id,
        reportVersion: baseline.version,
        changes,
      },
      {
        onSuccess: () => {
          showToast({
            variant: "success",
            title: "Entry updated",
            description: "The pre-submit check has been re-run.",
          });
          onClose();
        },
        onError: (err) => {
          showToast(expenseErrorToast(err, "Couldn’t save"));
          // Stale version: the form's baseline is out of date. Close it so
          // reopening starts from the refetched entry and version.
          if (isApiError(err) && err.code === "CONFLICT") onClose();
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="atlas-text-title text-foreground">
          Edit entry
          <span className="atlas-text-body ml-2 font-normal text-muted-foreground">
            {entry.vendor ?? CATEGORY_LABELS[entry.category]}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-4"
          onSubmit={handleSave}
          aria-label="Edit expense entry"
          noValidate
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field
              id={`${idp}-amount`}
              label={`Amount (${entry.currency})`}
              hint={
                mileageEntry
                  ? "Computed by Atlas from the trip (MIL-01)."
                  : undefined
              }
            >
              <Input
                id={`${idp}-amount`}
                inputMode="decimal"
                value={form.amount}
                onChange={(e) => set("amount")(e.target.value)}
                aria-invalid={error !== null && error.includes("amount")}
                readOnly={mileageEntry}
                aria-readonly={mileageEntry || undefined}
              />
              <AmountConversionNote
                conversion={entry.amount_conversion}
                className="mt-1"
                data-testid="entry-editor-amount-conversion"
              />
            </Field>
            <Field id={`${idp}-date`} label="Date">
              <Input
                id={`${idp}-date`}
                type="date"
                value={form.expenseDate}
                onChange={(e) => set("expenseDate")(e.target.value)}
              />
            </Field>
            <Field id={`${idp}-vendor`} label="Vendor">
              <Input
                id={`${idp}-vendor`}
                value={form.vendor}
                onChange={(e) => set("vendor")(e.target.value)}
              />
            </Field>
            <Field id={`${idp}-category`} label="Category">
              <select
                id={`${idp}-category`}
                value={form.category}
                onChange={(e) =>
                  set("category")(e.target.value as ExpenseCategory)
                }
                className={cn(FIELD_CLASS, "h-10")}
                disabled={mileageEntry}
              >
                {(mileageEntry ? [entry.category] : CATEGORIES).map((c) => (
                  <option key={c} value={c}>
                    {CATEGORY_LABELS[c]}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              id={`${idp}-description`}
              label="Description"
              className="sm:col-span-2"
            >
              <Input
                id={`${idp}-description`}
                value={form.description}
                onChange={(e) => set("description")(e.target.value)}
              />
            </Field>
            {showsAttendees(form.category) && (
              <AttendeesField
                idp={idp}
                category={form.category}
                value={form.attendees ?? ""}
                onChange={(v) => set("attendees")(v)}
              />
            )}
            {showsDisclosure(form.category) && form.disclosure && (
              <DisclosureFields
                idp={idp}
                category={form.category}
                value={form.disclosure}
                onChange={(v) => set("disclosure")(v)}
              />
            )}
            {mileageEntry && form.mileage && (
              <MileageFields
                idp={idp}
                value={form.mileage}
                onChange={(v) => set("mileage")(v)}
                stored={{ trip: entry.mileage, amount: String(entry.amount) }}
              />
            )}
            {recomputeMileage && (
              <p
                className="atlas-text-caption text-muted-foreground sm:col-span-2"
                data-testid="mileage-recompute-hint"
              >
                This line no longer matches the mileage policy (for example the
                MIL-01 rate changed). Save changes and Atlas recomputes the
                amount at the current rate.
              </p>
            )}
            <div className="flex min-w-0 flex-col gap-1.5 sm:col-span-2">
              <span className="atlas-text-body font-medium text-foreground">
                Receipt
              </span>
              {receiptIds.length === 0 ? (
                <span className="atlas-text-caption text-muted-foreground">
                  No receipt attached.
                </span>
              ) : (
                <ul className="flex flex-col gap-1">
                  {receiptIds.map((rid, i) => (
                    <li
                      key={rid}
                      data-testid={`entry-receipt-${i + 1}`}
                      className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 rounded-atlas-md border border-border px-3 py-1.5"
                    >
                      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                        <span className="atlas-text-caption inline-flex min-w-0 flex-wrap items-center gap-x-1.5 text-foreground">
                          <Paperclip
                            className="h-3.5 w-3.5 shrink-0"
                            aria-hidden="true"
                          />
                          <span className="font-medium">
                            {receiptIds.length > 1
                              ? receiptLabels[i]
                              : "Receipt attached"}
                          </span>
                          {receiptStates[i].kept_for_audit && (
                            <span
                              data-testid="receipt-kept-for-audit"
                              className="text-muted-foreground"
                              title={
                                receiptStates[i].removal_blocked_reason ??
                                undefined
                              }
                            >
                              · Kept for audit
                            </span>
                          )}
                          {!receiptStates[i].kept_for_audit &&
                            notReviewed[i] && (
                              <span
                                data-testid="receipt-not-reviewed"
                                className="text-muted-foreground"
                              >
                                · Not yet reviewed
                                {receiptStates[i].removable
                                  ? " · can be removed"
                                  : ""}
                              </span>
                            )}
                        </span>
                        {detailed && (
                          <span className="atlas-text-caption break-words text-muted-foreground">
                            {receiptSummaries[i] ??
                              "No vendor, date or total read from this receipt"}
                          </span>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <AtlasButton
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setViewingId(receiptIds[i] ?? null)}
                          aria-label={`View receipt ${i + 1}`}
                        >
                          <Eye className="h-4 w-4" aria-hidden="true" />
                          View
                        </AtlasButton>
                        {receiptStates[i].removable && (
                          <AtlasButton
                            type="button"
                            variant="ghost"
                            size="sm"
                            loading={
                              removeReceipt.isPending &&
                              removeReceipt.variables?.receiptId === rid
                            }
                            disabled={dirty}
                            disabledReason={
                              dirty
                                ? "Save or cancel your changes first"
                                : undefined
                            }
                            onClick={() =>
                              setConfirmRemove({
                                id: rid,
                                label:
                                  (receiptIds.length > 1
                                    ? receiptLabels[i]
                                    : "This receipt") +
                                  (receiptSummaries[i]
                                    ? ` (${receiptSummaries[i]})`
                                    : ""),
                              })
                            }
                            aria-label={`Remove receipt ${i + 1}`}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                            Remove
                          </AtlasButton>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              {anyKept && (
                <span className="atlas-text-caption text-muted-foreground">
                  {receiptIds.length > 1
                    ? "A receipt kept for audit was seen by a reviewer and can’t be removed. Attach the right receipt if one is wrong."
                    : "Kept for audit — a reviewer has seen this receipt. Attach the right receipt if this one is wrong."}
                </span>
              )}
              {viewing >= 0 && (
                <ReceiptViewerDialog
                  receiptId={receiptIds[viewing]}
                  title={receiptLabels[viewing]}
                  summary={
                    receiptSummaries[viewing] ??
                    (detailed
                      ? "No vendor, date or total read from this receipt."
                      : null)
                  }
                  onClose={() => setViewingId(null)}
                />
              )}
              {confirmRemove && (
                <Modal
                  open
                  variant="destructive"
                  onOpenChange={(open) => {
                    if (!open) setConfirmRemove(null);
                  }}
                  title="Remove this receipt?"
                  description={`${confirmRemove.label} will be removed from this entry and deleted (a receipt split across several lines stays on the others). This can’t be undone.`}
                  confirmLabel="Remove"
                  onConfirm={() => handleRemoveReceipt(confirmRemove.id)}
                />
              )}
              {onAttachReceipt && (
                <span>
                  <AtlasButton
                    type="button"
                    variant="secondary"
                    size="sm"
                    disabled={dirty}
                    disabledReason={
                      dirty ? "Save or cancel your changes first" : undefined
                    }
                    onClick={onAttachReceipt}
                  >
                    <Paperclip className="h-4 w-4" aria-hidden="true" />
                    Attach receipt
                  </AtlasButton>
                </span>
              )}
            </div>
            <Field
              id={`${idp}-justification`}
              label="Justification"
              className="sm:col-span-2"
              hint={
                justifiable
                  ? "Explain the policy exception. Your approver sees it with the flag; it lets you submit, it does not approve the exception."
                  : "Optional. Explains a policy exception to your approver. A missing receipt or a disallowed category must be fixed instead."
              }
            >
              {exceptions.length > 0 && (
                <ul className="flex flex-col gap-1">
                  {exceptions.map((f, i) => (
                    <li
                      key={i}
                      className="atlas-text-caption break-words text-[var(--color-warning)]"
                    >
                      {f.message}
                    </li>
                  ))}
                </ul>
              )}
              <textarea
                id={`${idp}-justification`}
                ref={justificationRef}
                rows={3}
                maxLength={JUSTIFICATION_MAX}
                value={form.justification}
                onChange={(e) => set("justification")(e.target.value)}
                placeholder="e.g. Conference hotel — every capped property nearby was sold out."
                className={FIELD_CLASS}
              />
            </Field>
            {groundTypeFlag && (
              <p
                className="atlas-text-caption break-words text-muted-foreground sm:col-span-2"
                data-testid="entry-ground-type-hint"
              >
                {CONFIRM_GROUND_TYPE_HINT}
              </p>
            )}
            {/* EXP-04: offered only where the server says the receipt is required
                and missing (a missing_receipt flag with affidavit_allowed) — the
                same condition the server enforces when signing. A signed
                affidavit stays visible so the owner can withdraw it. */}
            {(affidavitOffered || Boolean(entry.receipt_affidavit_at)) && (
              <div
                className="flex min-w-0 flex-col gap-1.5 sm:col-span-2"
                data-testid="entry-receipt-affidavit"
              >
                <label
                  htmlFor={`${idp}-affidavit`}
                  className="atlas-text-body flex items-start gap-2 text-foreground"
                >
                  <input
                    id={`${idp}-affidavit`}
                    type="checkbox"
                    checked={form.affidavit}
                    onChange={(e) => set("affidavit")(e.target.checked)}
                    className="mt-1 h-4 w-4 shrink-0"
                  />
                  <span>
                    I confirm the receipt for this expense was lost — sign the
                    missing-receipt affidavit. Explain what happened in the
                    justification above.
                  </span>
                </label>
                {affidavitCleared && (
                  <span
                    className="atlas-text-caption text-[var(--color-warning)]"
                    data-testid="entry-receipt-affidavit-cleared"
                    role="status"
                  >
                    You changed the expense, so the affidavit is no longer
                    signed. Tick it again to sign it for the edited expense.
                  </span>
                )}
                <span className="atlas-text-caption text-muted-foreground">
                  {AFFIDAVIT_CLAUSE} Above $75, or beyond the quarterly count,
                  the receipt is still required; your approver always reviews an
                  affidavit.
                </span>
                {receiptIds.length > 0 && (
                  <span
                    className="atlas-text-caption text-muted-foreground"
                    data-testid="entry-receipt-affidavit-receipted"
                  >
                    A receipt is now attached, so this affidavit is not used and
                    does not count toward the quarter. Untick it to withdraw it.
                  </span>
                )}
              </div>
            )}
          </div>

          {error && (
            <p
              role="alert"
              className="atlas-text-caption text-[var(--color-error)]"
            >
              {error}
            </p>
          )}

          <div className="flex flex-wrap justify-end gap-2">
            <AtlasButton
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Cancel
            </AtlasButton>
            <AtlasButton
              type="submit"
              variant="primary"
              size="sm"
              loading={update.isPending}
            >
              <Save className="h-4 w-4" aria-hidden="true" />
              Save changes
            </AtlasButton>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
