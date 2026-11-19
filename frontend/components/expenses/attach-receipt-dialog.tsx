"use client";

import * as React from "react";
import { Paperclip, Upload } from "lucide-react";

import { AtlasButton, Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { cn } from "@/lib/utils";
import {
  useAttachableReceipts,
  useAttachReceipt,
} from "@/lib/query/receipt-hooks";
import { useQueryClient } from "@tanstack/react-query";
import { fetchFreshReport, useLinkReceipt } from "@/lib/query/expense-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import { CATEGORY_LABELS, formatCurrency } from "@/lib/expenses/format";
import { computeSubmitGate } from "@/lib/expenses/submit-gate";
import { normalizePreSubmitFlags } from "@/lib/expenses/readiness";
import {
  ACCEPT_ATTR,
  isUploadRejection,
  uploadErrorMessage,
  validateReceiptFile,
} from "@/lib/receipts/validation";
import type {
  ExpenseEntry,
  ExpenseReport,
  PolicyVerdictSummary,
  Receipt,
} from "@/lib/types";

/**
 * Attach a receipt to an entry — UAT2 (Issue 1).
 *
 * Two ways, both server-checked against the report's version (If-Match):
 *   - upload a file: POST /receipts?expense_id= (OCR runs, then the receipt is
 *     linked under the report lock and the pre-submit check re-runs);
 *   - pick one of the caller's own, unattached receipts — or (UAT2 hardening)
 *     one already on another line of this report, to split an itemized
 *     receipt: POST /expenses/{id}/receipts.
 * The toast reports the server's refreshed verdict for THIS entry and the
 * report's refreshed submit gate; it never evaluates policy itself. Mount only
 * while open.
 */

function newKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `attach-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function receiptLabel(r: Receipt): string {
  const f = r.extracted_fields ?? {};
  const vendor = typeof f.vendor === "string" && f.vendor ? f.vendor : null;
  const date = typeof f.date === "string" && f.date ? f.date.slice(0, 10) : "";
  const amount =
    f.amount !== null && f.amount !== undefined && f.amount !== ""
      ? formatCurrency(
          String(f.amount),
          typeof f.currency === "string" && f.currency ? f.currency : "USD",
        )
      : "total not read";
  return [vendor ?? "Receipt", date, amount].filter(Boolean).join(" · ");
}

export interface AttachToast {
  variant: "success" | "info";
  title: string;
  description: string;
}

/**
 * Toast copy after an attach — UAT2 hardening (owner decision 3).
 *
 * "You can submit" vs "still blocked" comes from the REAL submit gate of the
 * report as the server re-checked it (`refreshed.pre_submit_flags` → readiness
 * level → `computeSubmitGate`, exactly what the Submit button uses) — never
 * from the entry's missing-receipt flag alone: another line, or another flag on
 * this line, can still block the report. `verdict` (the entry's refreshed flags)
 * only explains WHY the receipt itself didn't help. `refreshed` null (the report
 * couldn't be refetched / an idempotent replay) → a neutral toast that claims
 * nothing about submitting.
 */
export function attachOutcomeToast(
  verdict: PolicyVerdictSummary | null,
  refreshed: Pick<ExpenseReport, "pre_submit_flags"> | null,
): AttachToast {
  const types = verdict?.flag_types ?? [];
  if (
    verdict &&
    verdict.blocking_count > 0 &&
    types.includes("missing_receipt")
  ) {
    return {
      variant: "info",
      title: "Receipt attached, but it doesn’t satisfy the receipt rule",
      description:
        "Check the flag on this entry for the reason — the report can’t be submitted until it’s fixed.",
    };
  }
  if (!refreshed) {
    return {
      variant: "success",
      title: "Receipt attached",
      description: "The pre-submit check has been refreshed.",
    };
  }
  const readiness = normalizePreSubmitFlags(refreshed.pre_submit_flags);
  const gate = computeSubmitGate(readiness.level);
  // Blocking first: an advisory note (even "couldn't be re-run") never hides that
  // the report can't be submitted.
  if (!gate.canSubmit) {
    const n = readiness.issueCount;
    return {
      variant: "info",
      title: "Receipt attached — the report still can’t be submitted",
      description: `${n} ${n === 1 ? "issue still blocks" : "issues still block"} submitting. Fix ${n === 1 ? "it" : "them"} in the pre-submit check.`,
    };
  }
  // The re-run failure is the REPORT-level note: not_evaluated with no entry and
  // no rule. A rule's own "could not verify" advisory (MEAL-02, GRND-01 ...) is
  // not_evaluated too, but it names its entry and rule — the check DID run.
  const recheckFailed = readiness.flags.some(
    (f) => f.flagType === "not_evaluated" && !f.entryId && !f.ruleType,
  );
  if (recheckFailed) {
    return {
      variant: "info",
      title: "Receipt attached",
      description:
        "The pre-submit check couldn’t be re-run; your approver’s check will cover it.",
    };
  }
  if (gate.requiresConfirm) {
    const receiptReview =
      types.includes("missing_receipt") || types.includes("receipt_mismatch");
    return {
      variant: "info",
      title: "Receipt attached — you can submit",
      description: receiptReview
        ? "The receipt couldn’t be fully verified against this entry, so your approver will review it."
        : "Some items will be reviewed by your approver.",
    };
  }
  return {
    variant: "success",
    title: "Receipt attached — ready to submit",
    description: "The pre-submit check found no issues.",
  };
}

export interface AttachReceiptDialogProps {
  entry: ExpenseEntry;
  report: Pick<ExpenseReport, "id" | "version">;
  /** Ids of every line on this report — a picked receipt already on one of
   *  them will be split across both lines (itemized receipt). */
  reportEntryIds?: readonly string[];
  onOpenChange: (open: boolean) => void;
  onAttached?: () => void;
}

export function AttachReceiptDialog({
  entry,
  report,
  reportEntryIds = [],
  onOpenChange,
  onAttached,
}: AttachReceiptDialogProps) {
  const qc = useQueryClient();
  const upload = useAttachReceipt();
  const link = useLinkReceipt();
  const attachable = useAttachableReceipts(entry.id);
  const { showToast } = useAtlasToast();
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const busy = upload.isPending || link.isPending;
  const label =
    entry.vendor ?? entry.description ?? CATEGORY_LABELS[entry.category];

  const done = async (
    verdict: PolicyVerdictSummary | null,
    refreshed: ExpenseReport | null = null,
  ) => {
    onOpenChange(false);
    onAttached?.();
    // The toast follows the report's REAL refreshed submit gate (decision 3):
    // the link response carries the re-checked report; after an upload it is
    // fetched fresh. If that fails, the toast stays neutral.
    let fresh: ExpenseReport | null = refreshed;
    if (!fresh) {
      try {
        fresh = await fetchFreshReport(qc, report.id);
      } catch {
        fresh = null;
      }
    }
    showToast(attachOutcomeToast(verdict, fresh));
  };

  const fail = (err: unknown) => {
    showToast(expenseErrorToast(err, "Couldn’t attach the receipt"));
    onOpenChange(false);
  };

  const handleFile = (file: File | undefined) => {
    if (!file || busy) return;
    const check = validateReceiptFile(file);
    if (!check.valid) {
      setError(check.error ?? "This file can’t be used as a receipt.");
      return;
    }
    setError(null);
    upload.mutate(
      {
        entryId: entry.id,
        reportId: report.id,
        reportVersion: report.version,
        file,
        idempotencyKey: newKey(),
      },
      {
        onSuccess: (receipt) => void done(receipt.policy_verdict ?? null),
        // Phase 11: a file the server refuses (too large, wrong type, damaged,
        // over quota) is reported in place so another file can be picked.
        onError: (err) =>
          isUploadRejection(err)
            ? setError(uploadErrorMessage(err))
            : fail(err),
      },
    );
  };

  const handleConfirm = () => {
    if (busy) return;
    if (!selected) {
      setError("Choose a receipt, or upload a new one.");
      return;
    }
    setError(null);
    link.mutate(
      {
        entryId: entry.id,
        reportId: report.id,
        reportVersion: report.version,
        receiptId: selected,
      },
      {
        onSuccess: (res) => void done(res.policy_verdict, res.report),
        onError: fail,
      },
    );
  };

  const receipts = attachable.data ?? [];

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="Attach a receipt"
      description={`Upload the receipt for “${label} · ${formatCurrency(entry.amount, entry.currency)}”, or pick one you’ve already uploaded.`}
      confirmLabel={link.isPending ? "Attaching…" : "Attach selected"}
      onConfirm={handleConfirm}
    >
      <div className="mt-3 flex flex-col gap-3">
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT_ATTR}
          className="hidden"
          aria-label="Receipt file"
          data-testid="attach-receipt-file"
          onChange={(e) => {
            handleFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
        <AtlasButton
          type="button"
          variant="secondary"
          size="sm"
          loading={upload.isPending}
          disabled={link.isPending}
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="h-4 w-4" aria-hidden="true" />
          {upload.isPending ? "Reading receipt…" : "Upload a receipt file"}
        </AtlasButton>

        <fieldset className="flex flex-col gap-1.5">
          <legend className="atlas-text-body mb-1 font-medium text-foreground">
            Or choose a receipt you uploaded
          </legend>
          {attachable.isLoading ? (
            <p className="atlas-text-caption text-muted-foreground">
              Loading your receipts…
            </p>
          ) : receipts.length === 0 ? (
            <p className="atlas-text-caption text-muted-foreground">
              You have no receipts to attach.
            </p>
          ) : (
            <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
              {receipts.map((r) => (
                <li key={r.id}>
                  <label
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded-atlas-md border border-border px-3 py-2",
                      selected === r.id ? "bg-muted" : "bg-background",
                    )}
                  >
                    <input
                      type="radio"
                      name={`attach-${entry.id}`}
                      value={r.id}
                      checked={selected === r.id}
                      onChange={() => setSelected(r.id)}
                    />
                    <Paperclip
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                      aria-hidden="true"
                    />
                    <span className="flex min-w-0 flex-col">
                      <span className="atlas-text-body break-words text-foreground">
                        {receiptLabel(r)}
                      </span>
                      {r.expense_id &&
                        (r.shared_expense_ids?.includes(entry.id) ? (
                          <span className="atlas-text-caption text-muted-foreground">
                            Already split onto this line — attaching it again
                            re-checks this line
                          </span>
                        ) : reportEntryIds.includes(r.expense_id) ? (
                          <span className="atlas-text-caption text-muted-foreground">
                            On another line of this report — it will be split
                            across both lines (up to its total)
                          </span>
                        ) : (
                          <span className="atlas-text-caption text-muted-foreground">
                            From your Receipt Inbox — its draft line there will
                            be removed
                          </span>
                        ))}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>

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
