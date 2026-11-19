"use client";

import * as React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  Lock,
  ScanLine,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useAtlasToast } from "@/components/atlas";
import { cn } from "@/lib/utils";
import {
  receiptAmountNeedsInput,
  receiptNeedsReview,
  receiptReviewReasons,
  toFieldRows,
} from "@/lib/receipts/fields";
import { useCorrectReceiptField } from "@/lib/query/receipt-hooks";
import { isApiError } from "@/lib/types";
import type { Receipt, ReceiptFieldKey } from "@/lib/types";
import { FieldRow, FIELD_ROW_GRID } from "./field-row";
import { AmountConversionNote } from "@/components/atlas/amount-conversion-note";
import {
  SUPPORTED_CURRENCIES,
  normalizeSupportedCurrency,
} from "@/lib/money/fx";
import { ReceiptImage } from "./receipt-image";
import { pdfPagesNotice } from "@/lib/receipts/validation";

/**
 * Extraction-review panel — P5-T6.
 *
 * Image preview beside the per-field table (Field | Value | Confidence | Source).
 * The preview prefers the server's scoped, short-lived signed link (C2,
 * NFR-SEC4) and falls back to the local blob preview of the just-uploaded file
 * when the server has none (e.g. local storage / e2e).
 * Renders the pipeline's truth and persists inline corrections via PATCH; the
 * server flips provenance to 'user_edited' and the returned receipt re-renders the
 * row (and bubbles up so the queue item stays in sync).
 */

/**
 * C7R: the pipeline flagged this receipt for human review (e.g. the total is
 * not legible). The reasons are shown verbatim and prominently; when the
 * amount itself is in question the amount row is left empty for the user to
 * fill in (see receiptAmountNeedsInput / toFieldRows).
 */
function NeedsReviewNotice({ receipt }: { receipt: Receipt }) {
  if (!receiptNeedsReview(receipt)) return null;
  const reasons = receiptReviewReasons(receipt);
  const amountEntered = receipt.field_provenance?.amount === "user_edited";
  const amountWithheld = receiptAmountNeedsInput(receipt);
  return (
    <div
      role="alert"
      className="flex gap-3 rounded-atlas-md border border-[#FDE68A] bg-[#FFFBEB] p-[var(--space-3)]"
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-warning)]"
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="atlas-text-body font-medium text-foreground">
          This receipt needs your review
        </p>
        {reasons.length > 0 ? (
          <ul className="list-disc pl-4 atlas-text-caption text-foreground">
            {reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : (
          <p className="atlas-text-caption text-foreground">
            Some details could not be read reliably.
          </p>
        )}
        <p className="atlas-text-caption text-muted-foreground">
          {amountWithheld
            ? "Enter the total exactly as printed on the receipt. The amount has not been filled in for you."
            : amountEntered
              ? "Thanks — the total you entered will be used."
              : "Check the details listed above against the receipt before saving."}
        </p>
      </div>
    </div>
  );
}

/** Fallback copy if the server marks a receipt locked without a reason. */
export const RECEIPT_LOCKED_TEXT =
  "This receipt is part of a submitted report and can’t be changed — attach a new receipt instead.";

/**
 * UAT2 hardening: the receipt is evidence a reviewer relied on (part of a
 * submitted / returned report, or kept for audit). The server refuses any
 * correction (409 RECEIPT_LOCKED), so Edit is hidden and this explains why.
 */
function LockedNotice({ receipt }: { receipt: Receipt }) {
  if (!receipt.correction_locked) return null;
  return (
    <div
      role="status"
      data-testid="receipt-locked-notice"
      className="flex gap-3 rounded-atlas-md border border-border bg-muted p-[var(--space-3)]"
    >
      <Lock
        className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <p className="atlas-text-caption min-w-0 break-words text-foreground">
        {receipt.correction_locked_reason || RECEIPT_LOCKED_TEXT}
      </p>
    </div>
  );
}

/**
 * UAT2 defect D: the server says this line may be a second claim for one
 * purchase — a blocking `possible_duplicate`, or a receipt matching one that
 * backs (or backed) another claim (`duplicate_reason`).
 */
export function isPossibleDuplicate(
  verdict: NonNullable<Receipt["policy_verdict"]>,
): boolean {
  return (
    verdict.flag_types.includes("possible_duplicate") ||
    Boolean(verdict.duplicate_reason)
  );
}

/** "Possible duplicate: <server reason>" — or a generic line when none was sent. */
function duplicateText(
  verdict: NonNullable<Receipt["policy_verdict"]>,
): string {
  const reason = verdict.duplicate_reason?.trim();
  if (!reason) {
    return "Possible duplicate — this looks like an expense you’ve already claimed.";
  }
  const sentence = /[.!?]$/.test(reason) ? reason : `${reason}.`;
  return /^possible duplicate/i.test(sentence)
    ? sentence
    : `Possible duplicate: ${sentence}`;
}

/** One-line, server-derived summary of the drafted entry's policy check. */
export function policyVerdictText(
  verdict: NonNullable<Receipt["policy_verdict"]>,
): string {
  const blocking = Number(verdict.blocking_count) || 0;
  const advisory = Number(verdict.advisory_count) || 0;
  if (isPossibleDuplicate(verdict)) {
    // Named first: a second claim for one purchase is the issue to act on.
    return `${duplicateText(verdict)} Review it in the Receipt Inbox before submitting.`;
  }
  if (blocking > 0) {
    const missingReceipt = verdict.flag_types.includes("missing_receipt");
    return `${blocking} blocking policy issue${blocking === 1 ? "" : "s"}${
      missingReceipt
        ? " — a total you typed is not receipt evidence, so the receipt requirement is not met"
        : ""
    }. Review it in the Receipt Inbox before submitting.`;
  }
  if (!verdict.in_policy) {
    return "Not confirmed in policy. Review it in the Receipt Inbox before submitting.";
  }
  if (advisory > 0) {
    return `In policy, with ${advisory} note${advisory === 1 ? "" : "s"} for the approver.`;
  }
  return "In policy.";
}

/**
 * C7R: the draft expense the receipt created (on upload, or when the user
 * entered a withheld total) and the server's policy verdict for it. Rendered
 * as-is — the client never re-derives compliance.
 */
/**
 * Phase 12: a PDF with more pages than were rendered into the receipt image
 * (the server reads the first few only) — say so instead of dropping silently.
 */
function PdfPagesNotice({ receipt }: { receipt: Receipt }) {
  const text = pdfPagesNotice(receipt.pdf_pages_total, receipt.pdf_pages_used);
  if (!text) return null;
  return (
    <div
      role="status"
      data-testid="pdf-pages-notice"
      className="flex gap-3 rounded-atlas-md border border-border bg-muted/40 p-[var(--space-3)]"
    >
      <FileText
        className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <p className="atlas-text-caption text-foreground">{text}</p>
    </div>
  );
}

function DraftedExpenseNotice({ receipt }: { receipt: Receipt }) {
  const verdict = receipt.policy_verdict;
  if (!verdict || !receipt.expense_id) return null;
  const clean =
    verdict.in_policy && (Number(verdict.blocking_count) || 0) === 0;
  const Icon = clean ? CheckCircle2 : ShieldAlert;
  return (
    <div
      role="status"
      data-testid="drafted-expense-notice"
      className={cn(
        "flex gap-3 rounded-atlas-md border p-[var(--space-3)]",
        clean
          ? "border-[#BBF7D0] bg-[#F0FDF4]"
          : "border-[#FECACA] bg-[#FEF2F2]",
      )}
    >
      <Icon
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          clean ? "text-[var(--color-success)]" : "text-[var(--color-error)]",
        )}
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="atlas-text-body font-medium text-foreground">
          Draft expense added to your Receipt Inbox
        </p>
        <p className="atlas-text-caption text-foreground">
          {policyVerdictText(verdict)}
        </p>
      </div>
    </div>
  );
}

export interface ExtractionReviewProps {
  receipt: Receipt;
  previewUrl: string;
  fileName: string;
  onReceiptUpdate: (receipt: Receipt) => void;
  className?: string;
}

function ProcessingPathChip({ receipt }: { receipt: Receipt }) {
  const isGapFill = receipt.processing_path === "agent_gap_fill";
  if (!receipt.processing_path) return null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-atlas-full px-[10px] py-[6px] atlas-text-caption font-medium",
        isGapFill
          ? "bg-[var(--color-ai-surface)] text-[var(--color-ai-primary)]"
          : "bg-muted text-muted-foreground",
      )}
    >
      {isGapFill ? (
        <Sparkles className="h-3 w-3" aria-hidden="true" />
      ) : (
        <ScanLine className="h-3 w-3" aria-hidden="true" />
      )}
      {isGapFill ? "AI Gap-Fill" : "Deterministic OCR"}
    </span>
  );
}

export function ExtractionReview({
  receipt,
  previewUrl,
  fileName,
  onReceiptUpdate,
  className,
}: ExtractionReviewProps) {
  const rows = toFieldRows(receipt);
  // Phase 12: a PDF upload is shown as the PNG the server rendered from it.
  const fromPdf =
    receipt.source_format === "pdf" || fileName.toLowerCase().endsWith(".pdf");
  const correctMutation = useCorrectReceiptField();
  const { showToast } = useAtlasToast();
  const [savingKey, setSavingKey] = React.useState<ReceiptFieldKey | null>(
    null,
  );

  const handleSave = async (key: ReceiptFieldKey, raw: string) => {
    // Coerce a numeric amount to a number; otherwise send the value as typed.
    let value: unknown = raw;
    if (key === "currency") {
      // Product gap 8: only a currency the server can convert (it re-checks).
      const code = normalizeSupportedCurrency(raw);
      if (!code) {
        showToast({
          variant: "error",
          title: "Currency not supported",
          description: `Use one of: ${SUPPORTED_CURRENCIES.join(", ")}.`,
        });
        throw new Error("unsupported currency");
      }
      value = code;
    }
    if (key === "amount") {
      const num = Number(raw);
      if (raw.trim() !== "" && !Number.isNaN(num)) value = num;
      // C7R: on a needs-review receipt the user's typed total is the ONLY
      // source of the amount, so it must be a real positive number.
      if (
        receiptNeedsReview(receipt) &&
        (typeof value !== "number" || !Number.isFinite(value) || value <= 0)
      ) {
        showToast({
          variant: "error",
          title: "Enter the total as a number",
          description: "Type the amount printed on the receipt, e.g. 42.50.",
        });
        throw new Error("invalid amount");
      }
    }

    setSavingKey(key);
    try {
      const updated = await correctMutation.mutateAsync({
        receiptId: receipt.id,
        field: key,
        value,
      });
      onReceiptUpdate(updated);
      showToast(correctionToast(updated));
    } catch (err) {
      if (isApiError(err) && err.code === "RECEIPT_LOCKED") {
        // Submitted since this screen loaded: lock the review in place.
        onReceiptUpdate({
          ...receipt,
          correction_locked: true,
          correction_locked_reason: err.message || RECEIPT_LOCKED_TEXT,
        });
        showToast({
          variant: "info",
          title: "This receipt can’t be changed",
          description: err.message || RECEIPT_LOCKED_TEXT,
        });
        return;
      }
      showToast({
        variant: "error",
        title: "Couldn't save correction",
        description: isApiError(err) ? err.message : "Please try again.",
      });
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <div className={cn("flex flex-col gap-[var(--space-4)]", className)}>
      {/* Header: processing path */}
      <div className="flex items-center justify-between gap-3">
        <h2 className="atlas-text-subtitle font-medium text-foreground">
          Extracted fields
        </h2>
        <ProcessingPathChip receipt={receipt} />
      </div>

      <LockedNotice receipt={receipt} />
      {!receipt.correction_locked && <NeedsReviewNotice receipt={receipt} />}
      <DraftedExpenseNotice receipt={receipt} />
      <PdfPagesNotice receipt={receipt} />

      {/* Two-column: preview | table */}
      <div className="grid grid-cols-1 gap-[var(--space-5)] lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <ReceiptImage
          receiptId={receipt.id}
          fallbackUrl={previewUrl}
          fileName={fileName}
          fromPdf={fromPdf}
          className="lg:sticky lg:top-2 self-start"
        />

        {/* Size container: the field grid follows THIS element's width
            (see .atlas-field-table in styles/globals.css). */}
        <div className="atlas-field-table min-w-0">
          {/* Column header — shown only when the table is wide enough for
              four columns (container query), hidden in the stacked layout. */}
          <div
            className={cn(
              FIELD_ROW_GRID,
              "atlas-field-header border-b border-border pb-[var(--space-2)] atlas-text-caption font-semibold uppercase tracking-wide text-muted-foreground",
            )}
          >
            <span data-cell="label">Field</span>
            <span data-cell="value">Value</span>
            <span data-cell="confidence">Confidence</span>
            <span data-cell="source">Source</span>
          </div>

          {rows.map((row) => (
            <FieldRow
              key={row.key}
              row={row}
              grounding={receipt.grounding_context}
              onSave={handleSave}
              saving={savingKey === row.key}
              locked={Boolean(receipt.correction_locked)}
            />
          ))}
          <AmountConversionNote
            conversion={receipt.amount_conversion}
            className="mt-[var(--space-3)]"
            data-testid="receipt-amount-conversion"
          />
        </div>
      </div>
    </div>
  );
}

/**
 * UAT2 defect D: toast for a finished upload — only when the drafted line is a
 * possible duplicate (the queue keeps the first upload selected, so the notice
 * of a later one would go unseen).  Anything else is covered by the notice.
 */
export function uploadOutcomeToast(receipt: Receipt): {
  variant: "error";
  title: string;
  description: string;
} | null {
  const verdict = receipt.policy_verdict;
  if (!verdict || !receipt.expense_id || !isPossibleDuplicate(verdict)) {
    return null;
  }
  return {
    variant: "error",
    title: "Possible duplicate added to your Receipt Inbox",
    description: policyVerdictText(verdict),
  };
}

/** Toast for a saved correction, naming what happened to the linked expense. */
export function correctionToast(updated: Receipt): {
  variant: "success" | "info";
  title: string;
  description: string;
} {
  switch (updated.linked_expense_sync) {
    case "drafted":
      return {
        variant: "success",
        title: "Expense drafted",
        description: updated.policy_verdict
          ? `Added to your Receipt Inbox. ${policyVerdictText(updated.policy_verdict)}`
          : "Added to your Receipt Inbox.",
      };
    case "updated":
      return {
        variant: "success",
        title: "Field updated",
        description: "The linked expense and its report total were updated.",
      };
    case "skipped_report_not_editable":
      return {
        variant: "info",
        title: "Receipt updated",
        description:
          "The linked expense is on a report that is no longer a draft, so it was not changed.",
      };
    default:
      return {
        variant: "success",
        title: "Field updated",
        description: "Your correction was saved.",
      };
  }
}
