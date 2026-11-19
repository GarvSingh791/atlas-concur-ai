"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";

import { ReportList } from "./report-list";
import { ReportDetail } from "./report-detail";

/**
 * Expense Dashboard screen — P5-T7 (US-E4, US-E5).
 *
 * Single-screen orchestrator (mirrors the receipts/planner pattern): a list of
 * the employee's own reports, drilling into a detail view. The detail composes
 * the entries table (+ lineage), the pre-submit readiness panel, the submit gate,
 * and the rejection notice. The screen renders server truth only.
 */

/** Product gap 8: `/expenses?report=<id>` opens that report (card-feed link).
 *  The server still scopes the read; an unreadable id shows its error state. */
export function initialReportId(
  search: string | null | undefined,
): string | null {
  if (!search) return null;
  const id = new URLSearchParams(search).get("report")?.trim();
  return id ? id : null;
}

export function ExpensesScreen() {
  // Product gap 3: "/expenses?report=<id>" (global search, notifications) opens
  // that report's detail — read-only for a non-owner (ReportDetail decides).
  // Product gap 8 (card feed) uses the same link; initialReportId trims and
  // drops a blank id.
  const linkedReportId = initialReportId(useSearchParams()?.toString());
  const [selectedReportId, setSelectedReportId] = React.useState<string | null>(
    linkedReportId,
  );
  React.useEffect(() => {
    if (linkedReportId) setSelectedReportId(linkedReportId);
  }, [linkedReportId]);

  return (
    <div className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-6 p-4 sm:p-6">
      {selectedReportId === null ? (
        <>
          <header className="flex flex-col gap-1">
            <h1 className="atlas-text-display text-foreground">Expenses</h1>
            <p className="atlas-text-body text-muted-foreground">
              Review and submit your expense reports.
            </p>
          </header>
          <ReportList onSelectReport={setSelectedReportId} />
        </>
      ) : (
        <ReportDetail
          reportId={selectedReportId}
          onBack={() => setSelectedReportId(null)}
        />
      )}
    </div>
  );
}
