"use client";

import * as React from "react";
import { ArrowLeft } from "lucide-react";

import { ErrorState, LoadingState, StatusBadge } from "@/components/atlas";
import { EntriesTable } from "@/components/expenses/entries-table";
import {
  formatMoney,
  riskBadgeVariant,
  statusBadgeVariant,
  statusLabel,
} from "@/lib/approvals/format";
import {
  entryPolicyVerdicts,
  flagEntryLabel,
} from "@/lib/approvals/entry-policy";
import { toDecisionView } from "@/lib/approvals/risk-summary";
import type { ApprovalDetail } from "@/lib/types/approvals";
import { AiAnalysis } from "./ai-analysis";
import { DecisionActions } from "./decision-actions";
import { DecisionHistory } from "./decision-history";
import { GradeRequirementPanel } from "./grade-requirement";
import { ExceptionsList } from "./exceptions-list";
import { TripSegments } from "./trip-segments";

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-[var(--space-2)]">
      <h3 className="atlas-text-caption font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  );
}

interface DecisionPanelProps {
  approvalId: string | null;
  detail: ApprovalDetail | null;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  onBack: () => void;
  canDecide: boolean;
  /** Signed-in user's id (blocks self-approval in the decision actions). */
  currentUserId?: string | null;
}

export function DecisionPanel({
  approvalId,
  detail,
  isLoading,
  isError,
  onRetry,
  onBack,
  canDecide,
  currentUserId = null,
}: DecisionPanelProps) {
  if (!approvalId) {
    return (
      <div className="hidden h-full items-center justify-center p-[var(--space-6)] lg:flex">
        <p className="atlas-text-body text-muted-foreground">
          Select an approval to review its details.
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="p-[var(--space-5)]">
        <LoadingState variant="form" rows={5} />
      </div>
    );
  }

  if (isError || !detail) {
    return (
      <ErrorState
        heading="Couldn’t load this approval"
        subtext="Something went wrong fetching the decision details."
        onRetry={onRetry}
      />
    );
  }

  const entries = detail.expense_report?.entries ?? [];
  const view = toDecisionView(detail.risk_summary, detail.subject_type, (f) =>
    flagEntryLabel(f, entries),
  );
  // QA round: same status the queue shows ("withdrawn" for a returned subject
  // its owner withdrew; the approval itself stays changes_requested).
  const shownStatus = detail.display_status || detail.current_status;

  const isTrip = detail.subject_type === "trip";
  const subjectTitle = isTrip
    ? detail.trip
      ? `${detail.trip.trip.origin} → ${detail.trip.trip.primary_destination}`
      : "Trip"
    : (detail.expense_report?.report.title ?? "Expense report");
  const amount = isTrip
    ? (detail.trip?.trip.estimated_cost ?? null)
    : (detail.expense_report?.report.total_amount ?? null);
  const currency = isTrip
    ? (detail.trip?.trip.currency ?? null)
    : (detail.expense_report?.report.currency ?? null);
  const itemCount = isTrip
    ? (detail.trip?.segments.length ?? 0)
    : (detail.expense_report?.entries.length ?? 0);

  return (
    <div className="flex h-full flex-col gap-[var(--space-5)] overflow-y-auto p-[var(--space-5)]">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1 self-start atlas-text-caption text-muted-foreground hover:text-foreground lg:hidden"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" /> Back to queue
      </button>

      <header className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <h2 className="atlas-text-title text-foreground">{subjectTitle}</h2>
          <span className="atlas-text-caption text-muted-foreground">
            {detail.submitter_name ?? "—"} · {formatMoney(amount, currency)} ·{" "}
            {itemCount} {isTrip ? "segments" : "items"}
          </span>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusBadge variant={riskBadgeVariant(detail.risk_level)} />
          <StatusBadge
            variant={statusBadgeVariant(shownStatus)}
            label={statusLabel(shownStatus)}
          />
        </div>
      </header>

      <Section title="AI Analysis">
        <AiAnalysis analysis={view.aiAnalysis} currency={currency} />
      </Section>

      <Section title="Exceptions">
        <ExceptionsList
          exceptions={view.exceptions}
          inPolicyNote={view.inPolicyNote}
        />
      </Section>

      <Section title="Context">
        {isTrip
          ? detail.trip && <TripSegments segments={detail.trip.segments} />
          : detail.expense_report && (
              <EntriesTable
                entries={detail.expense_report.entries}
                policyVerdicts={entryPolicyVerdicts(
                  detail.expense_report.entries,
                  detail.risk_summary?.flags,
                )}
              />
            )}
      </Section>

      {detail.steps.length > 0 && (
        <Section title="Decision history">
          <DecisionHistory steps={detail.steps} />
        </Section>
      )}

      {detail.grade_requirement && (
        <Section title="Approver level">
          <GradeRequirementPanel requirement={detail.grade_requirement} />
        </Section>
      )}

      <Section title="Decision">
        <DecisionActions
          approval={detail}
          canDecide={canDecide}
          currentUserId={currentUserId}
        />
      </Section>
    </div>
  );
}
