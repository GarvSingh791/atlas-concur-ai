"use client";

import * as React from "react";
import { ShieldCheck } from "lucide-react";

import type { GradeRequirement } from "@/lib/types/approvals";

/**
 * Follow-up (grades): the approver level an approval needs — the required grade,
 * the clauses that require it, who it is routed to and whether the signed-in
 * viewer may decide. Renders server truth only.
 */
export function GradeRequirementBadge({ short }: { short: string }) {
  return (
    <span
      data-testid="grade-requirement-badge"
      className="inline-flex items-center gap-1 rounded-full border border-[var(--color-warning)] px-2 py-0.5 atlas-text-caption font-medium text-[var(--color-warning)]"
    >
      <ShieldCheck className="h-3 w-3" aria-hidden="true" />
      Requires {short}
    </span>
  );
}

export function GradeRequirementPanel({
  requirement,
}: {
  requirement: GradeRequirement;
}) {
  const clauses = requirement.clauses.length
    ? requirement.clauses.join(", ")
    : null;
  return (
    <div
      className="flex flex-col gap-[var(--space-2)]"
      data-testid="grade-requirement"
    >
      <div className="flex flex-wrap items-center gap-2">
        <GradeRequirementBadge short={requirement.required_short} />
        {clauses && (
          <span className="atlas-text-caption text-muted-foreground">
            Required by {clauses}
          </span>
        )}
      </div>
      <p className="atlas-text-body text-foreground">
        Only an approver of grade {requirement.required_title} or above can
        decide this item.
        {requirement.routed_to_name
          ? ` Routed to ${requirement.routed_to_name}.`
          : ""}
      </p>
      {requirement.rationale && (
        <p className="atlas-text-caption text-muted-foreground">
          {requirement.rationale}
        </p>
      )}
    </div>
  );
}
