import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// The preview fetches GET /receipts/{id} for a scoped signed image link (C2) —
// keep it hermetic (no real network) and default to "no servable link".
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import type { Receipt } from "@/lib/types";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  getMock.mockResolvedValue({ ...fixture(), image_access_url: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function fixture(): Receipt {
  return {
    id: "rcpt-5",
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
      amount: 0.95, // high → green
      currency: 0.92,
      date: 0.88,
      vendor: 0.85,
      location: 0.6, // below threshold → flagged
      expense_type: 0.9,
    },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
      vendor: "deterministic",
      location: "ai_inferred",
      expense_type: "user_edited",
    },
    grounding_context: {
      segments: [
        {
          id: "seg-1",
          type: "flight",
          location: "New York, NY",
          start: "2026-06-20",
          end: "2026-06-23",
        },
      ],
      calendar_events: [],
    },
    created_at: "2026-06-11T10:00:00Z",
  };
}

function renderReview(previewUrl = "") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <ExtractionReview
        receipt={fixture()}
        previewUrl={previewUrl}
        fileName="receipt.png"
        onReceiptUpdate={() => {}}
      />
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

describe("ExtractionReview", () => {
  it("formats the amount as money with its currency", () => {
    renderReview();
    expect(screen.getByText("$42.50")).toBeTruthy();
  });

  it("renders each provenance badge from field_provenance (exact label mapping)", () => {
    renderReview();
    expect(screen.getAllByText("OCR Extracted").length).toBeGreaterThan(0);
    expect(screen.getByText("AI Inferred")).toBeTruthy(); // location
    expect(screen.getByText("User Edited")).toBeTruthy(); // expense_type
  });

  it("flags the below-threshold field for review", () => {
    renderReview();
    expect(screen.getByText(/Flagged/i)).toBeTruthy(); // location (0.60)
  });

  it("shows the processing-path indicator", () => {
    renderReview();
    expect(screen.getByText("AI Gap-Fill")).toBeTruthy();
  });

  it("renders the gap-fill explainer for the AI-inferred field from real grounding_context", () => {
    const { container } = renderReview();
    expect(screen.getByText("Why this value?")).toBeTruthy();
    // The grounding facts come straight from the server context, not synthesized.
    expect(container.textContent).toContain("New York, NY");
    expect(container.textContent).toContain("Jun 20, 2026");
  });

  it("prefers the server's scoped signed image link when one is returned", async () => {
    getMock.mockResolvedValue({
      ...fixture(),
      image_access_url: "https://signed.test/org-1/abc?sig=1",
      image_access_expires_at: new Date(Date.now() + 300_000).toISOString(),
    });
    renderReview("blob:local-preview");

    expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-5");
    const img = await screen.findByAltText("Receipt preview of receipt.png");
    await vi.waitFor(() =>
      expect(img.getAttribute("src")).toBe(
        "https://signed.test/org-1/abc?sig=1",
      ),
    );
  });

  it("falls back to the local blob preview when the server has no link", async () => {
    const { qc } = renderReview("blob:local-preview");

    // Wait for the query to actually SETTLE with a null link — not merely be
    // in flight (while pending the blob is shown regardless).
    await vi.waitFor(() =>
      expect(qc.getQueryState(["receipt", "rcpt-5"])?.status).toBe("success"),
    );
    expect(
      qc.getQueryData<Receipt>(["receipt", "rcpt-5"])?.image_access_url,
    ).toBeNull();
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(
      screen.getByAltText("Receipt preview of receipt.png").getAttribute("src"),
    ).toBe("blob:local-preview");
  });
});
