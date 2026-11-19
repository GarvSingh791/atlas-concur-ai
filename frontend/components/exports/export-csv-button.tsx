"use client";

import * as React from "react";
import { Download } from "lucide-react";

import { AtlasButton, useAtlasToast } from "@/components/atlas";
import {
  downloadFile,
  todayStamp,
  type DownloadParams,
} from "@/lib/api/download";
import { isApiError } from "@/lib/types/api";

export interface ExportCsvButtonProps {
  /** API path of the CSV endpoint, e.g. "/approvals/export.csv". */
  path: string;
  /** The SAME filters the on-screen list uses (blank values are dropped). */
  params?: DownloadParams;
  /** Filename stem: atlas-<stem>-<YYYY-MM-DD>.csv (matches the server's). */
  filenameStem: string;
  disabled?: boolean;
}

/**
 * "Export CSV" (Product gap 8). Downloads through an authenticated fetch →
 * blob (the API needs the bearer token); an error toast on failure. The
 * server enforces finance-only access, the row cap and the audit entry.
 */
export function ExportCsvButton({
  path,
  params,
  filenameStem,
  disabled = false,
}: ExportCsvButtonProps) {
  const { showToast } = useAtlasToast();
  const [busy, setBusy] = React.useState(false);

  const run = async () => {
    setBusy(true);
    try {
      await downloadFile(
        path,
        params,
        `atlas-${filenameStem}-${todayStamp()}.csv`,
      );
    } catch (err) {
      showToast({
        variant: "error",
        title: "Export failed",
        description: isApiError(err)
          ? err.message
          : "The CSV could not be downloaded. Please try again.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <AtlasButton
      type="button"
      variant="secondary"
      size="md"
      onClick={run}
      disabled={disabled || busy}
      aria-busy={busy}
      data-testid="export-csv"
    >
      <Download className="h-4 w-4" aria-hidden="true" />
      {busy ? "Exporting…" : "Export CSV"}
    </AtlasButton>
  );
}
