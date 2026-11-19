import * as React from "react";

/** QA round: a deactivated team member stays listed but is marked. */
export function InactiveMarker() {
  return (
    <span
      className="inline-flex shrink-0 items-center whitespace-nowrap rounded-atlas-sm border border-border bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
      data-testid="team-member-inactive"
    >
      Inactive
    </span>
  );
}
