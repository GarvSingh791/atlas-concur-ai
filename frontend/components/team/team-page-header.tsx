"use client";

import { useSession } from "@/lib/auth/session-provider";

/** Role-aware heading: a manager's direct reports vs. finance's whole org. */
export function TeamPageHeader() {
  const { role } = useSession();
  const orgWide = role === "finance_admin";
  return (
    <header className="flex flex-col gap-1">
      <h1 className="atlas-text-display text-foreground">Team</h1>
      <p className="atlas-text-body text-muted-foreground">
        {orgWide
          ? "Everyone in your organization — open trips, reports in review, pending approvals and spend."
          : "Your direct reports — open trips, reports in review, pending approvals and spend."}
      </p>
    </header>
  );
}
