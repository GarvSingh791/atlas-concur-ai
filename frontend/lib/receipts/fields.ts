/**
 * Field-row mapper — P5-T6.
 *
 * Pure functions that turn a server `Receipt` into the per-field view-models the
 * extraction-review table renders. This is presentation only: it maps 1:1 from
 * extracted_fields / field_confidence / field_provenance and never invents a
 * field, computes a confidence, or sets a provenance.
 *
 * The 0.70 routing threshold and the 0.0–1.0 → 0–100 confidence scaling mirror
 * the backend (services/ocr_provider, ConfidenceIndicator expects 0–100).
 */

import type { Receipt, ReceiptFieldKey, ReceiptProvenance } from "@/lib/types";

/** Pipeline field keys, in the order the review table shows them. */
export const RECEIPT_FIELD_ORDER: ReceiptFieldKey[] = [
  "amount",
  "currency",
  "date",
  "vendor",
  "location",
  "expense_type",
];

/** Human labels for each field key. */
const FIELD_LABELS: Record<ReceiptFieldKey, string> = {
  amount: "Amount",
  currency: "Currency",
  date: "Date",
  vendor: "Vendor",
  location: "Location",
  expense_type: "Expense Type",
};

/** Below this raw confidence a field is flagged for review (server routing threshold). */
export const CONFIDENCE_THRESHOLD = 0.7;

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** A single review-table row, derived entirely from server data. */
export interface FieldRow {
  key: ReceiptFieldKey;
  label: string;
  /** Display-formatted value ("" when the field is absent). */
  value: string;
  /** Raw extracted value, for seeding the inline editor. */
  rawValue: unknown;
  hasValue: boolean;
  /** Confidence scaled to 0–100, or null when absent. */
  confidence: number | null;
  /** True when the raw confidence is below the 0.70 routing threshold. */
  belowThreshold: boolean;
  /** Provenance from field_provenance, or null when absent. */
  provenance: ReceiptProvenance | null;
  /**
   * C7R: the user must enter this value themselves (needs-review receipt, amount
   * not yet user-entered). The row shows no value and the editor starts empty.
   */
  requiresUserInput: boolean;
}

/** True when the server marked this receipt as needing human review. */
export function receiptNeedsReview(receipt: Receipt): boolean {
  return receipt.needs_review === true;
}

/** The server's review reasons, cleaned (strings only, trimmed, non-empty). */
export function receiptReviewReasons(receipt: Receipt): string[] {
  const reasons = Array.isArray(receipt.review_reasons)
    ? receipt.review_reasons
    : [];
  return reasons
    .filter((r): r is string => typeof r === "string")
    .map((r) => r.trim())
    .filter((r) => r.length > 0);
}

/** Backend `extracted_fields.review_flags` — {field: reason_code} for fields the
 *  pipeline WITHHELD (e.g. `{ amount: "total_illegible" }`). */
function reviewFlags(receipt: Receipt): Record<string, unknown> {
  const raw = receipt.extracted_fields?.review_flags;
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? (raw as Record<string, unknown>)
    : {};
}

/** Server reasons that concern the total (amount) or the whole receipt. The
 *  backend's amount messages all name "total"/"amount"; an unreadable receipt
 *  ("Receipt could not be read") covers every field. */
const AMOUNT_REASON = /\b(total|amount|receipt could not be read)\b/i;

/**
 * C7R: true when the AMOUNT itself is in question on a needs-review receipt —
 * then it is never prefilled from anything (OCR guess, AI gap-fill, line items)
 * and only a value the USER enters counts.
 *
 * needs_review is receipt-wide (a missing vendor also sets it), so the amount
 * is withheld only on amount-specific evidence, mirroring the backend's
 * receipt_review_status: OCR failed, a pipeline review flag on `amount`, no
 * amount value, amount confidence missing / below the 0.70 threshold, or an
 * amount/total review reason. A confidently-read total stays visible when only
 * another field (vendor, date, …) needs review.
 */
export function receiptAmountNeedsInput(receipt: Receipt): boolean {
  if (!receiptNeedsReview(receipt)) return false;
  if (receipt.field_provenance?.amount === "user_edited") return false;
  if (receipt.ocr_status === "failed") return true;
  const flag = reviewFlags(receipt).amount;
  if (typeof flag === "string" && flag.length > 0) return true;
  const value = receipt.extracted_fields?.amount;
  if (
    value === null ||
    value === undefined ||
    (typeof value === "string" && value.trim() === "")
  ) {
    return true;
  }
  const conf = receipt.field_confidence?.amount;
  if (typeof conf !== "number" || !(conf >= CONFIDENCE_THRESHOLD)) return true;
  return receiptReviewReasons(receipt).some((r) => AMOUNT_REASON.test(r));
}

function formatDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const [, y, m, d] = match;
  const monthIndex = Number(m) - 1;
  if (monthIndex < 0 || monthIndex > 11) return value;
  return `${MONTHS[monthIndex]} ${Number(d)}, ${y}`;
}

function formatMoney(value: unknown, currency?: string | null): string {
  const num = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(num)) return String(value ?? "");
  if (currency) {
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
      }).format(num);
    } catch {
      // Unknown currency code — fall through to a plain 2-dp number.
    }
  }
  return num.toFixed(2);
}

/** Format one field's raw value for display, by field type. */
export function formatFieldValue(
  key: ReceiptFieldKey,
  value: unknown,
  currency?: string | null,
): string {
  if (value === null || value === undefined) return "";
  if (key === "amount") return formatMoney(value, currency);
  if (key === "date" && typeof value === "string") return formatDate(value);
  return String(value);
}

/** Map a receipt into ordered per-field review rows. */
export function toFieldRows(receipt: Receipt): FieldRow[] {
  const fields = receipt.extracted_fields ?? {};
  const confidence = receipt.field_confidence ?? {};
  const provenance = receipt.field_provenance ?? {};
  const currency = typeof fields.currency === "string" ? fields.currency : null;

  const withholdAmount = receiptAmountNeedsInput(receipt);

  return RECEIPT_FIELD_ORDER.map((key) => {
    const requiresUserInput = key === "amount" && withholdAmount;
    const rawValue = requiresUserInput ? null : fields[key];
    const hasValue = rawValue !== null && rawValue !== undefined;
    const rawConfidence =
      !requiresUserInput && typeof confidence[key] === "number"
        ? confidence[key]
        : null;
    const fieldProvenance = requiresUserInput
      ? null
      : ((provenance[key] as ReceiptProvenance) ?? null);

    return {
      key,
      label: FIELD_LABELS[key],
      value: formatFieldValue(key, rawValue, currency),
      rawValue,
      hasValue,
      confidence:
        rawConfidence === null ? null : Math.round(rawConfidence * 100),
      belowThreshold:
        rawConfidence !== null && rawConfidence < CONFIDENCE_THRESHOLD,
      provenance: fieldProvenance,
      requiresUserInput,
    };
  });
}
