import { describe, it, expect, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

import { RejectionNotice } from "@/components/expenses/rejection-notice";
import { normalizePreSubmitFlags } from "@/lib/expenses/readiness";

afterEach(cleanup);

describe("RejectionNotice", () => {
  it("shows the report's policy flags (with citation) as the issues to resolve", () => {
    // The seeded rejected report's real pre_submit_flags shape.
    const readiness = normalizePreSubmitFlags({
      warnings: [
        {
          severity: "high",
          message: "Lodging rate exceeds policy limit",
          clause_ref: "§4.2",
        },
      ],
    });
    render(<RejectionNotice readiness={readiness} ownerView />);
    expect(screen.getByText("Report rejected")).toBeTruthy();
    expect(screen.getByText("Lodging rate exceeds policy limit")).toBeTruthy();
    expect(screen.getByText("§4.2")).toBeTruthy();
  });

  it("always shows the honest gap note about the reviewer's comment", () => {
    const readiness = normalizePreSubmitFlags({
      warnings: [{ severity: "high", message: "x", clause_ref: "§1" }],
    });
    render(<RejectionNotice readiness={readiness} ownerView />);
    expect(
      screen.getByText(/doesn’t expose approval decision notes to employees/i),
    ).toBeTruthy();
  });

  it("shows 'No reason recorded' when there are no flags (never fabricated)", () => {
    const readiness = normalizePreSubmitFlags(null);
    render(<RejectionNotice readiness={readiness} ownerView />);
    expect(screen.getByText(/No reason was recorded/i)).toBeTruthy();
  });
});

// UAT2 Issue 7 — the owner's "fix and resubmit" voice is for the owner only.
describe("RejectionNotice — owner vs non-owner copy", () => {
  const flagged = () =>
    normalizePreSubmitFlags({
      warnings: [
        { severity: "high", message: "Over the cap", clause_ref: "§4" },
      ],
    });

  it("owner, changes requested: editable-again + re-submit lead-in", () => {
    render(
      <RejectionNotice
        kind="changes_requested"
        readiness={flagged()}
        reviewerReason="Add the folio"
        ownerView
      />,
    );
    expect(screen.getByText(/This report is editable again/)).toBeTruthy();
    expect(screen.getByText(/goes back to your approver/)).toBeTruthy();
    expect(
      screen.getByText(
        /Resolve these policy issues, then this report can be re-submitted/,
      ),
    ).toBeTruthy();
  });

  it("non-owner, changes requested: neutral copy, no call to resubmit", () => {
    render(
      <RejectionNotice
        kind="changes_requested"
        readiness={flagged()}
        reviewerReason="Add the folio"
        ownerView={false}
      />,
    );
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Add the folio")).toBeTruthy();
    expect(screen.getByText("Over the cap")).toBeTruthy();
    expect(
      screen.getByText(
        "The approver sent this report back to its owner for changes.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Policy issues on this report:")).toBeTruthy();
    expect(screen.queryByText(/editable again/)).toBeNull();
    expect(screen.queryByText(/your approver/)).toBeNull();
    expect(screen.queryByText(/\bre-?submit/i)).toBeNull();
  });

  it("non-owner, flags listed below: no 'then resubmit'", () => {
    render(
      <RejectionNotice
        kind="changes_requested"
        readiness={flagged()}
        reviewerReason={null}
        listFlags={false}
        ownerView={false}
      />,
    );
    expect(
      screen.getByText("The issues are listed in the pre-submit check below."),
    ).toBeTruthy();
    expect(screen.queryByText(/\bre-?submit/i)).toBeNull();
  });

  it("non-owner, rejected: never says the report can be re-submitted", () => {
    render(<RejectionNotice readiness={flagged()} ownerView={false} />);
    expect(screen.getByText("Report rejected")).toBeTruthy();
    expect(screen.getByText("Policy issues on this report:")).toBeTruthy();
    expect(screen.queryByText(/\bre-?submit/i)).toBeNull();
    expect(screen.queryByText(/your reviewer/)).toBeNull();
  });
});
