import * as React from "react";

import { formatDate } from "@/lib/approvals/format";
import type {
  ApprovalDecisionValue,
  ApprovalStep,
} from "@/lib/types/approvals";

/**
 * Read-only decision history (US-F2 AC2) — the approval's recorded steps,
 * including the system auto_approve step (approver "System").
 */

const DECISION_LABEL: Record<ApprovalDecisionValue, string> = {
  approve: "Approved",
  reject: "Rejected",
  request_changes: "Requested changes",
  auto_approve: "Auto-approved",
};

export function DecisionHistory({ steps }: { steps: ApprovalStep[] }) {
  if (steps.length === 0) return null;

  return (
    <ol className="flex flex-col gap-[var(--space-3)]">
      {steps.map((s) => (
        <li key={s.id} className="flex flex-col gap-0.5">
          <span className="atlas-text-body text-foreground">
            <span className="font-medium">
              {DECISION_LABEL[s.decision] ?? s.decision}
            </span>{" "}
            <span className="text-muted-foreground">
              by {s.approver_name ?? "System"}
              {s.decided_at ? ` · ${formatDate(s.decided_at)}` : ""}
            </span>
          </span>
          {s.reason && (
            <span className="atlas-text-caption text-muted-foreground">
              “{s.reason}”
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}
