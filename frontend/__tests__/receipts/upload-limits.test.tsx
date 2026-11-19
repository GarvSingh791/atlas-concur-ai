/**
 * Phase 11 (public-demo security): client-side upload checks mirror the
 * server's, and the server's refusals (413 / 415 / 422 / quota) read clearly
 * and are not offered for a pointless retry.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  renderHook,
  act,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/types/api";
import {
  ACCEPT_ATTR,
  MAX_SIZE_BYTES,
  fileExtension,
  isUploadRejection,
  uploadErrorMessage,
  validateReceiptFile,
} from "@/lib/receipts/validation";
import {
  useUploadQueue,
  type QueueItem,
} from "@/lib/receipts/use-upload-queue";
import { ReceiptQueue } from "@/components/receipts/receipt-queue";

const postMock = api.post as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => "blob:mock");
  globalThis.URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  vi.clearAllMocks();
  cleanup();
});

function makeFile(name: string, type: string, size = 1024): File {
  const file = new File(["x"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function apiError(status: number, code: string, message: string): ApiError {
  return new ApiError({ code, message, correlation_id: "cid" }, status);
}

describe("validateReceiptFile mirrors the server", () => {
  it("accepts WebP, and the accept attribute lists it", () => {
    expect(validateReceiptFile(makeFile("r.webp", "image/webp")).valid).toBe(
      true,
    );
    expect(ACCEPT_ATTR).toContain("image/webp");
    expect(ACCEPT_ATTR).toContain(".webp");
  });

  it("rejects a declared type that disagrees with the extension", () => {
    const r = validateReceiptFile(makeFile("receipt.jpg", "image/png"));
    expect(r.valid).toBe(false);
    expect(r.error).toMatch(/don't match/i);
  });

  it("rejects a supported type with an unsupported extension", () => {
    expect(
      validateReceiptFile(makeFile("receipt.exe", "image/png")).valid,
    ).toBe(false);
    expect(
      validateReceiptFile(makeFile("receipt.png.html", "image/png")).valid,
    ).toBe(false);
  });

  it("rejects an unsupported extension when the browser gives no type", () => {
    expect(validateReceiptFile(makeFile("receipt.exe", "")).valid).toBe(false);
    expect(validateReceiptFile(makeFile("receipt.png", "")).valid).toBe(true);
    // An inherited object key is not an allowed extension.
    expect(validateReceiptFile(makeFile("a.constructor", "")).valid).toBe(
      false,
    );
  });

  it("judges a right-to-left-override name on its real extension", () => {
    // Renders as "invoiceexe.png"; really ends in ".exe".
    expect(fileExtension("invoice‮gnp.exe")).toBe("exe");
    expect(
      validateReceiptFile(makeFile("invoice‮gnp.exe", "image/png")).valid,
    ).toBe(false);
  });

  it("folds full-width characters before reading the extension", () => {
    expect(fileExtension("ｒｅｃｅｉｐｔ．ｐｎｇ")).toBe("png");
  });

  it("lets a generic octet-stream type fall back to the extension", () => {
    expect(
      validateReceiptFile(makeFile("scan.jpg", "application/octet-stream"))
        .valid,
    ).toBe(true);
    expect(
      validateReceiptFile(makeFile("scan", "application/octet-stream")).valid,
    ).toBe(false);
  });

  it("rejects GIF / HEIC / SVG", () => {
    for (const [name, type] of [
      ["a.gif", "image/gif"],
      ["a.heic", "image/heic"],
      ["a.svg", "image/svg+xml"],
    ]) {
      expect(validateReceiptFile(makeFile(name, type)).valid).toBe(false);
    }
  });

  it("caps the size at the server's 10MB", () => {
    expect(MAX_SIZE_BYTES).toBe(10 * 1024 * 1024);
    expect(
      validateReceiptFile(makeFile("a.png", "image/png", MAX_SIZE_BYTES + 1))
        .error,
    ).toMatch(/10MB/);
  });
});

describe("server refusals", () => {
  it.each([
    // Follow-up: the server's own 413 message is shown as-is.
    [
      413,
      "PAYLOAD_TOO_LARGE",
      "The receipt file is too large. The maximum size is 10 MB.",
      /^The receipt file is too large\. The maximum size is 10 MB\.$/,
    ],
    [413, "NETWORK_ERROR", "HTTP 413: Payload Too Large", /too large.*10MB/i],
    [
      415,
      "UNSUPPORTED_MEDIA_TYPE",
      "The file's declared type doesn't match its contents.",
      /doesn't match its contents/,
    ],
    [422, "INVALID_UPLOAD", "The image is damaged or incomplete.", /damaged/],
    [
      409,
      "UPLOAD_QUOTA_EXCEEDED",
      "You have reached the limit of 200 stored receipts.",
      /limit of 200/,
    ],
  ])("%i %s reads clearly and is not retryable", (status, code, msg, re) => {
    const err = apiError(status, code, msg);
    expect(uploadErrorMessage(err)).toMatch(re);
    expect(isUploadRejection(err)).toBe(true);
  });

  it("a stale-version conflict or a 5xx is still retryable", () => {
    expect(isUploadRejection(apiError(409, "CONFLICT", "stale"))).toBe(false);
    expect(isUploadRejection(apiError(500, "INTERNAL_SERVER_ERROR", "x"))).toBe(
      false,
    );
    expect(isUploadRejection(new Error("network"))).toBe(false);
  });

  it("a busy PDF reader (503) shows the server's copy and stays retryable", () => {
    const err = apiError(
      503,
      "PDF_RENDERER_BUSY",
      "The PDF reader is busy right now. Please try again in a moment, or upload a PNG or JPG of the receipt.",
    );
    expect(uploadErrorMessage(err)).toMatch(/PDF reader is busy/);
    expect(isUploadRejection(err)).toBe(false);
  });
});

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  // eslint-disable-next-line react/display-name
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("upload queue", () => {
  it("a refused file fails with the server's reason and no Retry", async () => {
    postMock.mockRejectedValueOnce(
      apiError(
        415,
        "UNSUPPORTED_MEDIA_TYPE",
        "The file's declared type doesn't match its contents.",
      ),
    );
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });
    act(() =>
      result.current.enqueue([new File(["x"], "r.png", { type: "image/png" })]),
    );
    await waitFor(() => expect(result.current.items[0]?.status).toBe("failed"));
    expect(result.current.items[0].error).toMatch(/doesn't match its contents/);
    expect(result.current.items[0].retryable).toBe(false);
  });

  it("a network failure stays retryable", async () => {
    postMock.mockRejectedValueOnce(new Error("network"));
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });
    act(() =>
      result.current.enqueue([new File(["x"], "r.png", { type: "image/png" })]),
    );
    await waitFor(() => expect(result.current.items[0]?.status).toBe("failed"));
    expect(result.current.items[0].retryable).toBe(true);
  });

  it("hides Retry for a refused file but keeps Remove", () => {
    const base: QueueItem = {
      id: "q1",
      file: new File(["x"], "big.png"),
      fileName: "big.png",
      idempotencyKey: "k",
      status: "failed",
      previewUrl: "",
      error: "File is too large. Maximum size is 10MB.",
      retryable: false,
    };
    render(
      <ReceiptQueue
        items={[
          base,
          {
            ...base,
            id: "q2",
            fileName: "net.png",
            retryable: true,
            error: "Upload failed. Please retry.",
          },
        ]}
        selectedId={null}
        onSelect={() => {}}
        onRetry={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry big.png" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove big.png" })).toBeTruthy();
    expect(screen.getByText(/Maximum size is 10MB/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry net.png" })).toBeTruthy();
  });
});
