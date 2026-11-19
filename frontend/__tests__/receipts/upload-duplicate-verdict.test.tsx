/**
 * UAT2 defect D — a re-upload is never announced as "In policy".
 *
 * Browser evidence: uploading the same taxi receipt twice on /receipts answered
 * the second upload with "Draft expense added to your Receipt Inbox / In policy."
 * The server now re-runs the inbox's pre-submit check on every upload and its
 * verdict carries `possible_duplicate` + `duplicate_reason`; the receipts screen
 * must say so — in the drafted-expense notice AND in a toast as the upload lands
 * (the queue keeps the FIRST upload selected, so the notice alone goes unseen).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const showToast = vi.fn();
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { ReceiptsScreen } from "@/components/receipts/receipts-screen";
import {
  policyVerdictText,
  uploadOutcomeToast,
} from "@/components/receipts/extraction-review";
import type { PolicyVerdictSummary, Receipt } from "@/lib/types";

const postMock = api.post as unknown as ReturnType<typeof vi.fn>;
const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

const REASON =
  "Possible duplicate claim for METRO YELLOW CAB: the same supplier, date and amount is already claimed on this report.";

const CLEAN: PolicyVerdictSummary = {
  in_policy: true,
  blocking_count: 0,
  advisory_count: 0,
  flag_types: [],
};
const DUPLICATE: PolicyVerdictSummary = {
  in_policy: false,
  blocking_count: 1,
  advisory_count: 0,
  flag_types: ["possible_duplicate"],
  duplicate_reason: REASON,
};

function taxi(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rcpt-taxi",
    organization_id: "org-1",
    uploaded_by: "user-1",
    expense_id: "exp-1",
    image_url: "org-1/taxi",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: {
      amount: 93.44,
      currency: "USD",
      date: "2026-06-10",
      vendor: "METRO YELLOW CAB",
    },
    field_confidence: {
      amount: 0.95,
      currency: 0.99,
      date: 0.95,
      vendor: 0.93,
    },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
      vendor: "deterministic",
    },
    created_at: "2026-11-11T10:00:00Z",
    policy_verdict: CLEAN,
    ...over,
  };
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => "blob:mock");
  globalThis.URL.revokeObjectURL = vi.fn();
  getMock.mockResolvedValue({ ...taxi(), image_access_url: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderScreen() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <ReceiptsScreen />
    </QueryClientProvider>,
  );
  const upload = (name = "taxi_metro_93_44.png") => {
    const input = utils.container.querySelector(
      'input[type="file"][multiple]',
    ) as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], name, { type: "image/png" })] },
    });
  };
  return { ...utils, upload };
}

describe("policyVerdictText — a possible duplicate", () => {
  it("names the duplicate (the server's reason), never 'In policy'", () => {
    const text = policyVerdictText(DUPLICATE);
    expect(text).toMatch(/possible duplicate/i);
    expect(text).toContain("METRO YELLOW CAB");
    expect(text).toMatch(/Receipt Inbox/);
    expect(text).not.toMatch(/In policy/);
    expect(text).not.toMatch(/^1 blocking policy issue/);
  });

  it("says duplicate from the flag type alone (no reason sent)", () => {
    const text = policyVerdictText({ ...DUPLICATE, duplicate_reason: null });
    expect(text).toMatch(/possible duplicate/i);
    expect(text).not.toMatch(/In policy/);
  });

  it("a reused receipt (advisory, escalating) is named too", () => {
    const text = policyVerdictText({
      in_policy: false,
      blocking_count: 0,
      advisory_count: 1,
      flag_types: ["receipt_mismatch"],
      duplicate_reason:
        "This receipt reads the same as one already backing a claim on 'NYC visit'.",
    });
    expect(text).toMatch(/possible duplicate/i);
    expect(text).toContain("NYC visit");
  });

  it("a clean verdict is unchanged", () => {
    expect(policyVerdictText(CLEAN)).toBe("In policy.");
  });
});

describe("uploadOutcomeToast", () => {
  it("warns about a duplicate upload", () => {
    const toast = uploadOutcomeToast(taxi({ policy_verdict: DUPLICATE }));
    expect(toast).not.toBeNull();
    expect(toast?.variant).toBe("error");
    expect(toast?.title).toMatch(/possible duplicate/i);
    expect(toast?.description).toContain("METRO YELLOW CAB");
  });

  it("stays quiet for a clean upload (the notice covers it)", () => {
    expect(uploadOutcomeToast(taxi())).toBeNull();
    expect(uploadOutcomeToast(taxi({ policy_verdict: null }))).toBeNull();
  });
});

describe("ReceiptsScreen — the second identical upload", () => {
  it("toasts the duplicate and the notice says so when selected", async () => {
    postMock.mockResolvedValueOnce(taxi()).mockResolvedValueOnce(
      taxi({
        id: "rcpt-taxi-2",
        expense_id: "exp-2",
        policy_verdict: DUPLICATE,
      }),
    );
    const { upload } = renderScreen();

    upload();
    await waitFor(() =>
      expect(screen.getByTestId("drafted-expense-notice").textContent).toMatch(
        /In policy\./,
      ),
    );
    expect(showToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/duplicate/i) }),
    );

    upload();
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "error",
          title: expect.stringMatching(/possible duplicate/i),
        }),
      ),
    );

    // Select the second upload: its notice never claims "In policy".
    fireEvent.click(screen.getAllByText("taxi_metro_93_44.png")[1]);
    await waitFor(() => {
      const notice = screen.getByTestId("drafted-expense-notice").textContent;
      expect(notice).toMatch(/possible duplicate/i);
      expect(notice).not.toMatch(/In policy/);
    });
  });
});
