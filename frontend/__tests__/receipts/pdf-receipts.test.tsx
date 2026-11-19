/**
 * Phase 12: PDF receipts, rasterise-only. The server renders the first pages
 * of a PDF into ONE PNG and keeps only that image, so the client accepts PDFs
 * again (validation, file pickers, upload-zone copy), never previews the PDF
 * itself (no local blob, no <embed>), labels the rendered image "from PDF",
 * says when only the first pages were read, and shows the server's friendly
 * 422 copy for encrypted / damaged PDFs as sent.
 *
 * Replaces the phase-11 pdf-refused.test.tsx (behaviour changed on purpose).
 */
import * as React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  renderHook,
  screen,
  fireEvent,
  act,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const showToast = vi.fn();
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), patch: vi.fn(), get: vi.fn() },
}));

import { api } from "@/lib/api/client";
import { UploadZone } from "@/components/receipts/upload-zone";
import { ImagePreview } from "@/components/receipts/image-preview";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import { useUploadQueue } from "@/lib/receipts/use-upload-queue";
import {
  ACCEPT_ATTR,
  ACCEPTED_TYPES,
  isPdfFile,
  pdfPagesNotice,
  uploadErrorMessage,
  validateReceiptFile,
} from "@/lib/receipts/validation";
import { ApiError } from "@/lib/types/api";
import type { Receipt } from "@/lib/types";

const postMock = api.post as unknown as ReturnType<typeof vi.fn>;
const getMock = api.get as unknown as ReturnType<typeof vi.fn>;
const createObjectURL = vi.fn(() => "blob:mock");

beforeEach(() => {
  globalThis.URL.createObjectURL = createObjectURL;
  globalThis.URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function file(name: string, type: string, body = "%PDF-1.7"): File {
  return new File([body], name, { type });
}

describe("PDF receipts are accepted", () => {
  it.each([
    ["receipt.pdf", "application/pdf"],
    ["receipt.pdf", ""],
    ["receipt.PDF", "application/octet-stream"],
    ["scan", "application/pdf"],
    ["statement.pdf", "application/x-pdf"],
    ["folio.pdf", "APPLICATION/PDF"],
  ])("%s (%s) passes client validation", (name, type) => {
    expect(validateReceiptFile(file(name, type))).toEqual({ valid: true });
  });

  it.each([
    ["receipt.pdf", "image/png"],
    ["scan.png", "application/pdf"],
    ["scan.jpg", "application/x-pdf"],
  ])("%s (%s) is a name / type mismatch", (name, type) => {
    const r = validateReceiptFile(file(name, type));
    expect(r.valid).toBe(false);
    expect(r.error).toMatch(/don't match/);
  });

  it("an empty or oversized PDF is still refused", () => {
    expect(
      validateReceiptFile(file("e.pdf", "application/pdf", "")).valid,
    ).toBe(false);
    const big = file("big.pdf", "application/pdf");
    Object.defineProperty(big, "size", { value: 10 * 1024 * 1024 + 1 });
    expect(validateReceiptFile(big).error).toMatch(/10MB/);
  });

  it("the file pickers offer PDF", () => {
    expect(ACCEPTED_TYPES).toContain("application/pdf");
    expect(ACCEPT_ATTR).toMatch(/application\/pdf/);
    expect(ACCEPT_ATTR).toMatch(/\.pdf/);
  });

  it("the upload zone advertises PNG, JPG or PDF", () => {
    render(<UploadZone onFiles={() => {}} />);
    expect(screen.getByText(/PNG, JPG or PDF · up to 10 MB/)).toBeTruthy();
  });

  it("dropping a PDF reaches the upload queue", () => {
    const onFiles = vi.fn();
    render(<UploadZone onFiles={onFiles} />);
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const pdf = file("receipt.pdf", "application/pdf");
    fireEvent.change(input, { target: { files: [pdf] } });
    expect(onFiles).toHaveBeenCalledWith([pdf]);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("recognises a PDF by type or extension", () => {
    expect(isPdfFile(file("a.pdf", ""))).toBe(true);
    expect(isPdfFile(file("a", "application/pdf"))).toBe(true);
    expect(isPdfFile(file("a.png", "image/png"))).toBe(false);
  });
});

describe("a PDF is only ever shown as the rendered image", () => {
  function wrapper() {
    const qc = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    // eslint-disable-next-line react/display-name
    return ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
  }

  it("the upload queue makes no local preview of a PDF", async () => {
    postMock.mockResolvedValue({
      id: "r1",
      organization_id: "o",
      uploaded_by: "u",
      expense_id: null,
      image_url: "o/x-from-pdf.png",
      ocr_status: "processed",
      processing_path: "deterministic",
      extracted_fields: { amount: 10, currency: "USD" },
      field_confidence: { amount: 0.95 },
      field_provenance: { amount: "deterministic" },
      created_at: "2026-11-15T10:00:00Z",
      source_format: "pdf",
    } satisfies Receipt);
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: wrapper(),
    });
    act(() => result.current.enqueue([file("receipt.pdf", "application/pdf")]));
    await waitFor(() =>
      expect(result.current.items[0]?.status).toBe("processed"),
    );
    expect(result.current.items[0].previewUrl).toBe("");
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("the upload queue still previews an image locally", () => {
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: wrapper(),
    });
    postMock.mockReturnValue(new Promise(() => {}));
    act(() => result.current.enqueue([file("r.png", "image/png", "x")]));
    expect(result.current.items[0].previewUrl).toBe("blob:mock");
  });

  it("the preview renders an <img> with a 'from PDF' label, never an embed", () => {
    render(
      <ImagePreview
        url="https://signed.example/x-from-pdf.png"
        fileName="folio.pdf"
        fromPdf
      />,
    );
    expect(screen.getByTestId("from-pdf-badge").textContent).toBe("from PDF");
    expect(screen.getByRole("img")).toBeTruthy();
    expect(document.querySelector("embed, object, iframe")).toBeNull();
    // Zoom / rotate work on the rendered page like on any image.
    expect(screen.getByLabelText("Zoom in")).toBeTruthy();
  });

  it("the review labels the PDF and says only the first pages were read", () => {
    getMock.mockResolvedValue({ image_access_url: null });
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <ExtractionReview
          receipt={{
            id: "r1",
            organization_id: "o",
            uploaded_by: "u",
            expense_id: null,
            image_url: "o/x-from-pdf.png",
            ocr_status: "processed",
            processing_path: "deterministic",
            extracted_fields: { amount: 10, currency: "USD" },
            field_confidence: { amount: 0.95, currency: 0.95 },
            field_provenance: {
              amount: "deterministic",
              currency: "deterministic",
            },
            created_at: "2026-11-15T10:00:00Z",
            source_format: "pdf",
            pdf_pages_total: 12,
            pdf_pages_used: 3,
          }}
          previewUrl=""
          fileName="statement.pdf"
          onReceiptUpdate={() => {}}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("pdf-pages-notice").textContent).toMatch(
      /12 pages; only the first 3 were read/,
    );
    expect(document.querySelector("embed, object, iframe")).toBeNull();
  });

  it("an image upload has no 'from PDF' label", () => {
    render(<ImagePreview url="blob:x" fileName="r.png" />);
    expect(screen.queryByTestId("from-pdf-badge")).toBeNull();
  });
});

describe("PDF copy", () => {
  it("says when only the first pages were read", () => {
    expect(pdfPagesNotice(12, 3)).toBe(
      "This PDF has 12 pages; only the first 3 were read. Upload the other pages separately if they matter.",
    );
    expect(pdfPagesNotice(2, 1)).toMatch(/only the first 1 was read/);
    expect(pdfPagesNotice(3, 3)).toBeNull();
    expect(pdfPagesNotice(null, null)).toBeNull();
  });

  it.each([
    "This PDF is password-protected. Remove the password (or print it to a new PDF) and upload it again.",
    "We couldn't read this PDF. It may be damaged — export it again, or upload a PNG or JPG of the receipt.",
  ])("the server's 422 copy is shown as sent: %s", (message) => {
    const err = new ApiError(
      { code: "INVALID_UPLOAD", message, correlation_id: "cid" },
      422,
    );
    expect(uploadErrorMessage(err)).toBe(message);
  });
});
