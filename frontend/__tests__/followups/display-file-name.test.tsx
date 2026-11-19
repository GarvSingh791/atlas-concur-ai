/**
 * Follow-up (UI polish) — client file names (and OCR'd vendor text) are
 * sanitised for DISPLAY: bidi overrides/isolates, zero-width and C0/C1 control
 * characters are stripped, whitespace collapsed, long names truncated with an
 * ellipsis that keeps the extension visible. Applied wherever such text renders.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const showToast = vi.fn();
vi.mock("@/components/atlas", async (orig) => ({
  ...(await orig<typeof import("@/components/atlas")>()),
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useReceipt: () => ({ data: undefined }),
}));
vi.mock("@/components/receipts/receipt-image", () => ({
  ReceiptImage: () => null,
}));

import {
  cleanDisplayText,
  displayFileName,
  displayText,
} from "@/lib/receipts/display-name";
import { ReceiptQueue } from "@/components/receipts/receipt-queue";
import { ImagePreview } from "@/components/receipts/image-preview";
import { UploadZone } from "@/components/receipts/upload-zone";
import { ReceiptViewerDialog } from "@/components/expenses/receipt-viewer-dialog";
import { receiptSummary } from "@/components/expenses/entry-editor";
import { entryReceipts } from "@/components/expenses/entry-receipts";
import type { QueueItem } from "@/lib/receipts/use-upload-queue";

afterEach(() => {
  cleanup();
  showToast.mockReset();
});

const RLO = "\u202e";
const SPOOF = `invoice${RLO}gnp.exe`; // renders as "invoiceexe.png"
// Every character the sanitiser must remove (bidi, zero-width, controls).
const HOSTILE =
  "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u0000\u0007\u001b\u007f\u0085\u009b";
const BAD =
  /[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/;

describe("displayFileName / displayText", () => {
  it("strips bidi, zero-width and control characters", () => {
    expect(displayFileName(SPOOF)).toBe("invoicegnp.exe");
    const cleaned = cleanDisplayText(`a${HOSTILE}b`);
    expect(cleaned).not.toMatch(BAD);
    expect(cleaned.replace(/\s/g, "")).toBe("ab");
  });

  it("collapses whitespace (tabs, newlines, runs) and trims", () => {
    expect(displayText("  Hotel \t\n  Chicago   ")).toBe("Hotel Chicago");
    expect(displayFileName("my\n\nreceipt   final.png")).toBe(
      "my receipt final.png",
    );
  });

  it("truncates a long name with an ellipsis and keeps the extension", () => {
    const long = `${"a".repeat(200)}.jpeg`;
    const shown = displayFileName(long, 30);
    expect(Array.from(shown)).toHaveLength(30);
    expect(shown.endsWith("….jpeg")).toBe(true);
  });

  it("never splits a surrogate pair when truncating", () => {
    const shown = displayText("😀".repeat(50), 10);
    expect(Array.from(shown)).toHaveLength(10);
    expect(shown).not.toMatch(/[\ud800-\udbff](?![\udc00-\udfff])/);
  });

  it("keeps only the basename and never returns an empty name", () => {
    expect(displayFileName("C:\\Users\\x\\scan.png")).toBe("scan.png");
    expect(displayFileName(`${RLO}\u200b`)).toBe("Untitled file");
  });
});

function queueItem(over: Partial<QueueItem> = {}): QueueItem {
  return {
    id: "q1",
    file: new File(["x"], SPOOF),
    fileName: SPOOF,
    idempotencyKey: "k",
    status: "failed",
    error: "Upload failed",
    previewUrl: "",
    ...over,
  };
}

describe("rendered client file names are sanitised", () => {
  it("receipt queue: name, title and button labels", () => {
    render(
      <ReceiptQueue
        items={[queueItem()]}
        selectedId={null}
        onSelect={() => {}}
        onRetry={() => {}}
        onRemove={() => {}}
      />,
    );
    const name = screen.getByTestId("queue-file-name");
    expect(name.textContent).toBe("invoicegnp.exe");
    expect(name.getAttribute("title")).toBe("invoicegnp.exe");
    expect(screen.getByRole("button", { name: "Retry invoicegnp.exe" }));
    expect(screen.getByRole("button", { name: "Remove invoicegnp.exe" }));
    expect(document.body.innerHTML).not.toContain(RLO);
  });

  it("image preview toolbar and alt text", () => {
    render(<ImagePreview url="blob:x" fileName={SPOOF} />);
    expect(screen.getByText("invoicegnp.exe")).toBeTruthy();
    expect(screen.getByAltText("Receipt preview of invoicegnp.exe"));
    expect(document.body.innerHTML).not.toContain(RLO);
  });

  it("upload-zone rejection toast", () => {
    render(<UploadZone onFiles={() => {}} />);
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const bad = new File(["x"], `evil${RLO}fdp.png`, {
      type: "application/pdf",
    });
    fireEvent.change(input, { target: { files: [bad] } });
    expect(showToast).toHaveBeenCalledTimes(1);
    const { description } = showToast.mock.calls[0][0] as {
      description: string;
    };
    expect(description.startsWith("evilfdp.png: ")).toBe(true);
    expect(description).not.toContain(RLO);
  });

  it("receipt viewer dialog title and summary", () => {
    render(
      <ReceiptViewerDialog
        receiptId="r1"
        title={`Receipt${RLO} 2`}
        summary={`Hotel\u200b ${RLO}Chicago\n· $10.00`}
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("heading", { name: "Receipt 2" })).toBeTruthy();
    expect(screen.getByText("Hotel Chicago · $10.00")).toBeTruthy();
    expect(document.body.innerHTML).not.toContain(RLO);
  });
});

describe("entry editor receipt list", () => {
  it("the receipt summary shows a cleaned vendor", () => {
    const summary = receiptSummary(
      {
        receipt_id: "r1",
        link: "direct",
        vendor: `\u2067Hilton${RLO} Chicago\u2069`,
        receipt_date: null,
        amount: null,
        currency: null,
        covers_line: null,
        attached_after_return: false,
      },
      "USD",
    );
    expect(summary).toBe("Hilton Chicago");
    expect(typeof entryReceipts).toBe("function");
  });
});
