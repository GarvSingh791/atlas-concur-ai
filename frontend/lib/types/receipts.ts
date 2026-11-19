/**
 * Receipt DTO mirrors — P5-T6.
 *
 * Mirrors the backend `ReceiptResponse` (backend/app/modules/receipt/schemas.py)
 * 1:1. The screen renders the pipeline's truth: it never invents a field, never
 * computes confidence, and never sets provenance — the server owns all of that.
 *
 * Contract notes (verified against the real backend):
 *   - POST /receipts is SYNCHRONOUS — the response already carries the extracted
 *     fields (ocr_status may be 'processed' or 'failed'); no polling.
 *   - PATCH /receipts/{id} flips field_provenance[field]='user_edited' server-side.
 *   - image_url is an opaque object-storage key (NOT a usable/public URL).
 *   - GET /receipts/{id} (owner, owner's manager, finance) additionally carries a
 *     short-lived signed image_access_url + image_access_expires_at (NFR-SEC4,
 *     TTL <= 300 s). Null on POST/PATCH and when no servable link exists.
 *   - grounding_context is stored in the DB but is NOT currently exposed on
 *     ReceiptResponse; the optional field below is forward-compatible so the
 *     gap-fill explainer can render real grounding once the backend serializes it.
 */

import type { AmountConversion } from "@/lib/money/fx";

/** receipts.ocr_status enum. */
export type OcrStatus = "pending" | "processed" | "failed";

/** receipts.processing_path enum. */
export type ProcessingPath = "deterministic" | "agent_gap_fill";

/** field_provenance values — identical to the design-system ProvenanceType. */
export type ReceiptProvenance = "deterministic" | "ai_inferred" | "user_edited";

/** The known extracted-field keys (RECEIPT_FIELD_KEYS on the backend). */
export type ReceiptFieldKey =
  | "amount"
  | "currency"
  | "date"
  | "vendor"
  | "location"
  | "expense_type";

/** extracted_fields JSONB — known keys typed, extra keys tolerated. */
export interface ExtractedFields {
  amount?: number | string | null;
  currency?: string | null;
  date?: string | null;
  vendor?: string | null;
  location?: string | null;
  expense_type?: string | null;
  [key: string]: unknown;
}

/** field_confidence JSONB — per-field 0.0–1.0 floats. */
export type FieldConfidence = Record<string, number>;

/** field_provenance JSONB — per-field provenance enum. */
export type FieldProvenance = Record<string, ReceiptProvenance>;

/** A trip segment the gap-fill agent grounded an inference on. */
export interface GroundingSegment {
  id: string;
  type: string;
  location: string | null;
  start: string | null;
  end: string | null;
}

/** A calendar event the gap-fill agent grounded an inference on. */
export interface GroundingCalendarEvent {
  id: string;
  title: string;
  location: string | null;
  start: string | null;
  end: string | null;
}

/**
 * grounding_context JSONB — the itinerary/calendar facts the gap-fill agent used.
 * NOTE: not currently returned by ReceiptResponse (see file header).
 */
export interface GroundingContext {
  segments?: GroundingSegment[];
  calendar_events?: GroundingCalendarEvent[];
}

/**
 * Compact policy verdict for the auto-drafted expense created from a receipt
 * upload (backend FEATURE B). Mirrors `PolicyVerdictSummary` on the backend.
 *
 * Present on the POST /receipts response when the upload created a draft
 * expense (OCR read the total, or the user entered it), and on the PATCH
 * response when entering a withheld total created the deferred draft
 * (`linked_expense_sync: "drafted"`). The authoritative flag detail lives
 * on the linked ExpenseReport.pre_submit_flags and surfaces through the existing
 * expense-readiness panel + approval flow — this is just an at-a-glance badge.
 */
export interface PolicyVerdictSummary {
  in_policy: boolean;
  blocking_count: number;
  advisory_count: number;
  flag_types: string[];
  requires_approver?: string | null;
  /**
   * UAT2 defect D: why the line looks like a second claim for one purchase (a
   * `possible_duplicate`, or its receipt matching one that backs / backed
   * another claim) — the server's own flag reason, shown verbatim.
   */
  duplicate_reason?: string | null;
}

/**
 * Outcome of propagating a PATCH /receipts/{id} correction to the linked expense
 * entry (backend `LinkedExpenseSync`, receipt/schemas.py). Set ONLY on the PATCH
 * response.
 */
export type LinkedExpenseSync =
  | "updated"
  | "no_linked_expense"
  | "not_applicable"
  | "unchanged"
  /** C7R-18: entering a withheld total created the deferred draft entry. */
  | "drafted"
  | "skipped_report_not_editable"
  | "skipped_not_owner"
  /** UAT2: POST /receipts?expense_id= attached the upload to that entry. */
  | "attached";

/** Mirror of backend ReceiptResponse. */
export interface Receipt {
  id: string;
  organization_id: string;
  uploaded_by: string;
  expense_id: string | null;
  /** Object-storage key (NOT a public URL). Never construct a MinIO URL from this. */
  image_url: string;
  ocr_status: OcrStatus;
  processing_path: ProcessingPath | null;
  extracted_fields: ExtractedFields | null;
  field_confidence: FieldConfidence | null;
  field_provenance: FieldProvenance | null;
  /** Forward-compatible: present once the backend serializes it. */
  grounding_context?: GroundingContext | null;
  created_at: string;
  /**
   * Set on the POST /receipts response when the upload auto-drafted an expense
   * and ran the policy engine (FEATURE B); null/absent otherwise.
   */
  policy_verdict?: PolicyVerdictSummary | null;
  /** PATCH: what happened to the linked expense entry (C7-17 / C7R-18);
   *  POST ?expense_id= (UAT2): "attached". */
  linked_expense_sync?: LinkedExpenseSync | null;
  /**
   * Short-lived signed GET link to the image (NFR-SEC4) — set ONLY by
   * GET /receipts/{id}. Treat as a secret: never log, persist or share it.
   */
  image_access_url?: string | null;
  /** ISO timestamp at (or before) which image_access_url stops working. */
  image_access_expires_at?: string | null;
  /**
   * Link lifetime in seconds. Refresh scheduling uses this relative value (from
   * when the response arrived), so a skewed browser clock cannot cause a
   * refetch storm or a late refresh.
   */
  image_access_expires_in?: number | null;
  /**
   * Phase 12: "pdf" when the stored image was rendered from an uploaded PDF
   * (server-derived; the PDF itself is never stored or served). Optional so
   * older backends keep working.
   */
  source_format?: "image" | "pdf" | null;
  /** POST /receipts only: the sanitised original PDF name (display only). */
  source_filename?: string | null;
  /** POST /receipts only: pages in the PDF / pages shown in the image. */
  pdf_pages_total?: number | null;
  pdf_pages_used?: number | null;
  /**
   * C7R: true when the pipeline could not establish the receipt reliably (e.g.
   * the total is not legible). The review UI must surface `review_reasons` and
   * must NOT prefill the amount — the user enters the total. Optional so older
   * backends (field absent) keep working.
   */
  needs_review?: boolean | null;
  /** Human-readable reasons the receipt needs review (shown verbatim). */
  review_reasons?: string[] | null;
  /**
   * UAT2 hardening: the receipt is evidence a reviewer relied on (part of a
   * submitted / returned report, or kept for audit). The server refuses any
   * correction (409 RECEIPT_LOCKED) — the review UI hides Edit and shows
   * `correction_locked_reason`. Server-derived; absent on older backends.
   */
  correction_locked?: boolean | null;
  correction_locked_reason?: string | null;
  /**
   * UAT2 hardening: the OTHER lines (besides `expense_id`) this receipt backs
   * as an itemized split of one folio across lines of the same report.
   * Server-derived; absent on older backends.
   */
  shared_expense_ids?: string[] | null;
  /**
   * Product gap 8: the extracted amount in the org's base currency when the
   * receipt is in another one (server-computed, simulated reference rate).
   */
  amount_conversion?: AmountConversion | null;
}

/** PATCH /receipts/{id} body — value only; the server owns the provenance flip.
 *  `field` mirrors the backend's CorrectableReceiptField literal
 *  (receipt/schemas.py); any other key is rejected with 422. */
export interface ReceiptCorrection {
  field: ReceiptFieldKey;
  value: unknown;
}
