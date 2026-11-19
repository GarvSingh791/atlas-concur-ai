"use client";

import * as React from "react";
import { Info, X } from "lucide-react";

import {
  type DemoResetSchedule,
  usePublicConfig,
} from "@/lib/query/public-config-hooks";

/** sessionStorage key: the banner stays dismissed for the rest of the session. */
export const DEMO_BANNER_DISMISSED_KEY = "atlas.demoBanner.dismissed";

function readDismissed(): boolean {
  try {
    return window.sessionStorage.getItem(DEMO_BANNER_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** Local "HH:MM" of an ISO timestamp; null when absent or unparseable. */
export function formatResetTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * "Demo data resets daily — next reset ~HH:MM" (phase 12), in the visitor's
 * local time. Null when the server runs no scheduled reset.
 */
export function resetScheduleText(
  reset: DemoResetSchedule | undefined,
): string | null {
  if (!reset?.enabled) return null;
  const hours = reset.interval_hours;
  const cadence =
    hours === 24 || hours === null
      ? "daily"
      : hours === 1
        ? "every hour"
        : `every ${hours} hours`;
  const at = formatResetTime(reset.next_reset_at);
  return at
    ? `Demo data resets ${cadence} — next reset ~${at}.`
    : `Demo data resets ${cadence}.`;
}

/**
 * "Demo environment" banner — phase 11. Shown only when the server reports
 * DEMO_MODE (GET /public/config). Dismissible per browser session. A hint only:
 * every demo protection is enforced server-side.
 */
export function DemoBanner() {
  const config = usePublicConfig();
  const [dismissed, setDismissed] = React.useState(true);

  React.useEffect(() => setDismissed(readDismissed()), []);

  if (!config.data?.demo_mode || dismissed) return null;
  const resetText = resetScheduleText(config.data.demo_reset);

  const dismiss = () => {
    try {
      window.sessionStorage.setItem(DEMO_BANNER_DISMISSED_KEY, "1");
    } catch {
      // storage unavailable — dismiss for this render tree only
    }
    setDismissed(true);
  };

  return (
    <div
      role="status"
      data-testid="demo-banner"
      className="flex items-start gap-2 border-b border-border bg-secondary px-4 py-2 text-sm text-foreground"
    >
      <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <p className="flex-1">
        <span className="font-medium">Demo environment.</span> This is a shared
        public demo: the demo accounts are protected, and all data is
        periodically reset to the original sample data. Don&rsquo;t enter real
        or personal information.
        {resetText ? (
          <span data-testid="demo-reset-schedule"> {resetText}</span>
        ) : null}
      </p>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss demo notice"
        className="rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
