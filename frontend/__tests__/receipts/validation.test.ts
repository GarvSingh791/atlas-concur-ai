import { describe, it, expect } from "vitest";
import {
  validateReceiptFile,
  MAX_SIZE_BYTES,
  ACCEPTED_TYPES,
} from "@/lib/receipts/validation";

function makeFile(name: string, type: string, size: number): File {
  const file = new File(["x"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("validateReceiptFile", () => {
  it("accepts a PNG within the size limit", () => {
    const result = validateReceiptFile(makeFile("r.png", "image/png", 1024));
    expect(result.valid).toBe(true);
    expect(result.error).toBeUndefined();
  });

  it("accepts a JPG", () => {
    expect(
      validateReceiptFile(makeFile("r.jpg", "image/jpeg", 2048)).valid,
    ).toBe(true);
  });

  it("accepts a PDF (phase 12: rendered to an image by the server)", () => {
    const result = validateReceiptFile(
      makeFile("r.pdf", "application/pdf", 2048),
    );
    expect(result).toEqual({ valid: true });
  });

  it("accepts a file at exactly the 10MB boundary", () => {
    expect(
      validateReceiptFile(makeFile("r.png", "image/png", MAX_SIZE_BYTES)).valid,
    ).toBe(true);
  });

  it("rejects an unsupported type", () => {
    const result = validateReceiptFile(makeFile("r.txt", "text/plain", 100));
    expect(result.valid).toBe(false);
    // The copy lists the server allow-list (phase 12: PDF again).
    expect(result.error).toMatch(/PNG, JPG, WebP or PDF/i);
  });

  it("rejects a file over 10MB", () => {
    const result = validateReceiptFile(
      makeFile("big.png", "image/png", MAX_SIZE_BYTES + 1),
    );
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/10\s?MB/i);
  });

  it("rejects an empty file", () => {
    const result = validateReceiptFile(makeFile("empty.png", "image/png", 0));
    expect(result.valid).toBe(false);
  });

  it("recognizes a supported file by extension when the MIME type is blank", () => {
    expect(validateReceiptFile(makeFile("scan.jpg", "", 500)).valid).toBe(true);
  });

  it("exposes the accepted MIME types", () => {
    expect(ACCEPTED_TYPES).toContain("image/png");
    expect(ACCEPTED_TYPES).toContain("image/jpeg");
    expect(ACCEPTED_TYPES).toContain("application/pdf");
  });
});
