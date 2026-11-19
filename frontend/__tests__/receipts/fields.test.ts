import { describe, it, expect } from "vitest";
import {
  RECEIPT_FIELD_ORDER,
  toFieldRows,
  formatFieldValue,
} from "@/lib/receipts/fields";
import type { Receipt } from "@/lib/types";

function makeReceipt(overrides: Partial<Receipt> = {}): Receipt {
  return {
    id: "rcpt-1",
    organization_id: "org-1",
    uploaded_by: "user-1",
    expense_id: null,
    image_url: "org-1/abc",
    ocr_status: "processed",
    processing_path: "agent_gap_fill",
    extracted_fields: {
      amount: 42.5,
      currency: "USD",
      date: "2026-06-11",
      vendor: "Marriott",
      location: "New York, NY",
      expense_type: "hotel",
    },
    field_confidence: {
      amount: 0.95,
      currency: 0.92,
      date: 0.88,
      vendor: 0.85,
      location: 0.6,
      expense_type: 0.55,
    },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
      vendor: "deterministic",
      location: "ai_inferred",
      expense_type: "user_edited",
    },
    created_at: "2026-06-11T10:00:00Z",
    ...overrides,
  };
}

describe("RECEIPT_FIELD_ORDER", () => {
  it("is the six pipeline field keys in canonical order", () => {
    expect(RECEIPT_FIELD_ORDER).toEqual([
      "amount",
      "currency",
      "date",
      "vendor",
      "location",
      "expense_type",
    ]);
  });
});

describe("toFieldRows", () => {
  it("produces one row per field key, in order", () => {
    const rows = toFieldRows(makeReceipt());
    expect(rows.map((r) => r.key)).toEqual(RECEIPT_FIELD_ORDER);
  });

  it("scales confidence from 0.0–1.0 to 0–100", () => {
    const rows = toFieldRows(makeReceipt());
    const amount = rows.find((r) => r.key === "amount")!;
    expect(amount.confidence).toBe(95);
  });

  it("flags a field below the 0.70 routing threshold", () => {
    const rows = toFieldRows(makeReceipt());
    const location = rows.find((r) => r.key === "location")!; // 0.60
    const amount = rows.find((r) => r.key === "amount")!; // 0.95
    expect(location.belowThreshold).toBe(true);
    expect(amount.belowThreshold).toBe(false);
  });

  it("passes provenance through 1:1 from field_provenance", () => {
    const rows = toFieldRows(makeReceipt());
    expect(rows.find((r) => r.key === "location")!.provenance).toBe(
      "ai_inferred",
    );
    expect(rows.find((r) => r.key === "expense_type")!.provenance).toBe(
      "user_edited",
    );
  });

  it("formats the amount as money with its currency", () => {
    const rows = toFieldRows(makeReceipt());
    expect(rows.find((r) => r.key === "amount")!.value).toBe("$42.50");
  });

  it("formats the date as a human-readable day", () => {
    const rows = toFieldRows(makeReceipt());
    expect(rows.find((r) => r.key === "date")!.value).toBe("Jun 11, 2026");
  });

  it("marks missing fields as empty with no confidence/provenance", () => {
    const rows = toFieldRows(
      makeReceipt({
        extracted_fields: { amount: 42.5, currency: "USD" },
        field_confidence: { amount: 0.95, currency: 0.92 },
        field_provenance: {
          amount: "deterministic",
          currency: "deterministic",
        },
      }),
    );
    const vendor = rows.find((r) => r.key === "vendor")!;
    expect(vendor.hasValue).toBe(false);
    expect(vendor.value).toBe("");
    expect(vendor.confidence).toBeNull();
    expect(vendor.provenance).toBeNull();
  });

  it("tolerates null extracted_fields / confidence / provenance", () => {
    const rows = toFieldRows(
      makeReceipt({
        extracted_fields: null,
        field_confidence: null,
        field_provenance: null,
      }),
    );
    expect(rows).toHaveLength(6);
    expect(rows.every((r) => !r.hasValue)).toBe(true);
    expect(rows.every((r) => r.confidence === null)).toBe(true);
  });
});

describe("formatFieldValue", () => {
  it("formats money without currency as a plain 2-dp number", () => {
    expect(formatFieldValue("amount", 42.5, null)).toBe("42.50");
  });

  it("returns string fields unchanged", () => {
    expect(formatFieldValue("vendor", "Marriott")).toBe("Marriott");
  });

  it("returns an empty string for null/undefined", () => {
    expect(formatFieldValue("vendor", null)).toBe("");
    expect(formatFieldValue("location", undefined)).toBe("");
  });
});
