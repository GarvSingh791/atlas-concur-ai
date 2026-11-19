import * as React from "react";

import { TeamPageHeader } from "@/components/team/team-page-header";
import { TeamScreen } from "@/components/team/team-screen";

/**
 * Team (Product gap 5, PRD US-M3) — a manager's direct reports (finance: the
 * organization). The nav item + route gate key on expense.read.reports /
 * expense.read.org (nav-config); the server scopes and enforces every read.
 */
export default function TeamPage() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6">
      <TeamPageHeader />
      <TeamScreen />
    </div>
  );
}
