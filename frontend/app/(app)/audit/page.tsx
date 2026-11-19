import * as React from "react";

import { AuditTrail } from "@/components/audit/audit-trail";

/**
 * Audit trail screen (UI_UX.md → Magnifier / Audit, `GET /audit`).
 *
 * Finance-scoped: the nav entry is gated to finance_admin (nav-config) and the
 * endpoint itself enforces `audit.read` + organization scoping server-side.
 */
export default function AuditPage() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6">
      <header className="flex flex-col gap-1">
        <h1 className="atlas-text-display text-foreground">Audit Trail</h1>
        <p className="atlas-text-body text-muted-foreground">
          Every recorded state change across your organization, most recent
          first.
        </p>
      </header>
      <AuditTrail />
    </div>
  );
}
