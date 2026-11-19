/**
 * C7R issue 4b — expense reports now move to approved / rejected after an
 * approval decision, and the lifecycle can grow on the server. Every status
 * badge / label / filter must handle every value without crashing; an unknown
 * value renders a neutral badge with a humanized label (never a false
 * "Pending").
 */
import { describe, it, expect, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

import { StatusBadge, type StatusBadgeVariant } from "@/components/atlas";
import { ReportStatusBadge } from "@/components/expenses/report-status-badge";
import { QueueWidget } from "@/components/dashboard/widgets/queue-widget";
import {
  REPORT_STATUSES,
  REPORT_STATUS_LABELS,
  reportStatusLabel,
} from "@/lib/expenses/format";
import { statusBadgeVariant, statusLabel } from "@/lib/approvals/format";
import type { ApprovalQueueItem } from "@/lib/types/analytics";

afterEach(cleanup);

describe("C7R-4b report status rendering", () => {
  it("every known report status renders its label", () => {
    for (const s of REPORT_STATUSES) {
      render(<ReportStatusBadge status={s} />);
      expect(screen.getByText(REPORT_STATUS_LABELS[s])).toBeTruthy();
      cleanup();
    }
    expect(REPORT_STATUSES).toEqual(
      expect.arrayContaining([
        "draft",
        "submitted",
        "approved",
        "rejected",
        "reimbursed",
      ]),
    );
  });

  it("an unknown report status renders a neutral, humanized badge", () => {
    render(<ReportStatusBadge status="changes_requested" />);
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(reportStatusLabel("")).toBe("Unknown");
    expect(reportStatusLabel(undefined)).toBe("Unknown");
  });

  it("StatusBadge never crashes on an unknown variant", () => {
    render(
      <StatusBadge variant={"under_audit" as unknown as StatusBadgeVariant} />,
    );
    expect(screen.getByText("Under audit")).toBeTruthy();
  });

  it("approval status helpers: unknown → neutral (not 'pending')", () => {
    expect(statusBadgeVariant("approved")).toBe("approved");
    expect(statusBadgeVariant("escalated")).toBe("neutral");
    expect(statusLabel("escalated")).toBe("Escalated");
    expect(statusLabel("changes_requested")).toBe("Changes requested");
  });

  it("the dashboard queue widget labels every approval status truthfully", () => {
    const base = {
      subject_type: "expense_report",
      submitter_name: "A",
      subject_title: "T",
      subject_amount: "1.00",
      currency: "USD",
      risk_level: "low",
      created_at: "2026-06-12T00:00:00Z",
    };
    const items = [
      { ...base, id: "1", status: "auto_approved" },
      { ...base, id: "2", status: "changes_requested" },
      { ...base, id: "3", status: "escalated" },
    ] as unknown as ApprovalQueueItem[];
    render(
      <QueueWidget items={items} total={3} isLoading={false} isError={false} />,
    );
    expect(screen.getByText("Auto-approved")).toBeTruthy();
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Escalated")).toBeTruthy();
    expect(screen.queryByText("Pending")).toBeNull();
  });
});
