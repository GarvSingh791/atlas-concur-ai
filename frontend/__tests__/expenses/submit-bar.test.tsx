import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import { SubmitBar } from "@/components/expenses/submit-bar";
import { ApiError } from "@/lib/types/api";
import type { ExpenseReport } from "@/lib/types";
import type { ReadinessSummary } from "@/lib/expenses/readiness";

// Mock the toast + submit mutation so we can assert behavior in isolation.
const { showToast, mutate } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mutate: vi.fn(),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/query/expense-hooks", () => ({
  useSubmitReport: () => ({ mutate, isPending: false }),
}));

afterEach(cleanup);
beforeEach(() => {
  showToast.mockReset();
  mutate.mockReset();
});

function draftReport(): ExpenseReport {
  return {
    id: "rep-1",
    organization_id: "org-1",
    owner_id: "emp-1",
    trip_id: null,
    title: "June Miscellaneous",
    status: "draft",
    total_amount: "73.00",
    currency: "USD",
    submitted_at: null,
    pre_submit_flags: null,
    created_by: "emp-1",
    updated_by: "emp-1",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    version: 3,
  };
}

const READY: ReadinessSummary = {
  level: "ready",
  flags: [],
  issueCount: 0,
  reviewCount: 0,
};
const REVIEW: ReadinessSummary = {
  level: "review",
  flags: [
    {
      severity: "review",
      message: "soft",
      clauseRef: null,
      entryRef: null,
      flagType: null,
    },
  ],
  issueCount: 0,
  reviewCount: 1,
};
const ISSUE: ReadinessSummary = {
  level: "issue",
  flags: [
    {
      severity: "issue",
      message: "hard",
      clauseRef: null,
      entryRef: null,
      flagType: null,
    },
  ],
  issueCount: 1,
  reviewCount: 0,
};

function submitButton() {
  return screen
    .getByText("Submit report")
    .closest("button") as HTMLButtonElement;
}

describe("SubmitBar gating", () => {
  it("green: submit is enabled", () => {
    render(<SubmitBar report={draftReport()} readiness={READY} />);
    expect(submitButton().disabled).toBe(false);
  });

  it("red: submit is disabled", () => {
    render(<SubmitBar report={draftReport()} readiness={ISSUE} />);
    expect(submitButton().disabled).toBe(true);
  });

  it("amber: clicking opens a warning-confirmation modal before submitting", () => {
    render(<SubmitBar report={draftReport()} readiness={REVIEW} />);
    fireEvent.click(submitButton());
    expect(screen.getByText("Submit with warnings?")).toBeTruthy();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("green: clicking submits directly with the report id + version (If-Match)", () => {
    render(<SubmitBar report={draftReport()} readiness={READY} />);
    fireEvent.click(submitButton());
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({ id: "rep-1", version: 3 });
  });

  it("does not render for non-draft reports", () => {
    const submitted = { ...draftReport(), status: "submitted" as const };
    const { container } = render(
      <SubmitBar report={submitted} readiness={READY} />,
    );
    expect(container.textContent).toBe("");
  });
});

describe("SubmitBar version-conflict handling", () => {
  it("surfaces a 409 CONFLICT as an error toast (no silent overwrite)", () => {
    mutate.mockImplementation(
      (_args: unknown, opts: { onError: (e: unknown) => void }) => {
        opts.onError(
          new ApiError(
            {
              code: "CONFLICT",
              message: "Record was modified concurrently; reload and retry.",
              correlation_id: "abc",
            },
            409,
          ),
        );
      },
    );
    render(<SubmitBar report={draftReport()} readiness={READY} />);
    fireEvent.click(submitButton());
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast.mock.calls[0][0]).toMatchObject({
      variant: "error",
      title: "This report changed",
    });
  });

  it("toasts success when the submit resolves", () => {
    mutate.mockImplementation(
      (_args: unknown, opts: { onSuccess: () => void }) => opts.onSuccess(),
    );
    render(<SubmitBar report={draftReport()} readiness={READY} />);
    fireEvent.click(submitButton());
    expect(showToast.mock.calls[0][0]).toMatchObject({
      variant: "success",
      title: "Report submitted",
    });
  });
});
