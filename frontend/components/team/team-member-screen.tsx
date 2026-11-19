"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Briefcase,
  ChevronDown,
  ChevronRight,
  CreditCard,
} from "lucide-react";

import {
  EmptyState,
  ErrorState,
  KPICard,
  LoadingState,
} from "@/components/atlas";
import { ReportDetail } from "@/components/expenses/report-detail";
import { ReportStatusBadge } from "@/components/expenses/report-status-badge";
import { InactiveMarker } from "@/components/team/inactive-marker";
import { TripApprovalPanel } from "@/components/trips/trip-approval-panel";
import { TripSegmentList } from "@/components/trips/trip-segment-list";
import { formatMoney } from "@/lib/approvals/format";
import { useTeamMember } from "@/lib/query/team-hooks";
import { tripTitle } from "@/lib/trips/summary";
import { isApiError, type PaginatedResponse } from "@/lib/types/api";
import type { ReportListItem } from "@/lib/types/expenses";
import type { TripListItem } from "@/lib/types/trips";
import { cn } from "@/lib/utils";

type Tab = "trips" | "reports";

function Pager({
  page,
  label,
  onPage,
}: {
  page: PaginatedResponse<unknown>;
  label: string;
  onPage: (n: number) => void;
}) {
  if (page.pages <= 1) return null;
  return (
    <nav
      aria-label={`${label} pages`}
      className="flex items-center justify-end gap-2 atlas-text-caption"
    >
      <button
        type="button"
        className="rounded-atlas-md border border-border px-2 py-1 disabled:opacity-50"
        disabled={page.page <= 1}
        onClick={() => onPage(page.page - 1)}
      >
        Previous
      </button>
      <span>
        Page {page.page} of {page.pages}
      </span>
      <button
        type="button"
        className="rounded-atlas-md border border-border px-2 py-1 disabled:opacity-50"
        disabled={page.page >= page.pages}
        onClick={() => onPage(page.page + 1)}
      >
        Next
      </button>
    </nav>
  );
}

function TeamTripCard({
  trip,
  travelerName,
}: {
  trip: TripListItem;
  travelerName: string;
}) {
  const [open, setOpen] = React.useState(false);
  const detailsId = `team-trip-${trip.id}`;
  return (
    <TripApprovalPanel
      trip={{ ...trip, traveler_name: travelerName }}
      title={tripTitle(trip)}
    >
      <button
        type="button"
        className="atlas-text-caption flex w-fit items-center gap-1 font-medium text-[var(--color-primary-600)] hover:underline"
        aria-expanded={open}
        aria-controls={detailsId}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? (
          <ChevronDown className="h-4 w-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        )}
        {open ? "Hide trip details" : "Show trip details"}
      </button>
      {open && (
        <div id={detailsId}>
          <TripSegmentList
            tripId={trip.id}
            estimatedCost={trip.estimated_cost ?? null}
          />
        </div>
      )}
    </TripApprovalPanel>
  );
}

function ReportRow({
  report,
  onOpen,
}: {
  report: ReportListItem;
  onOpen: (id: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(report.id)}
      data-testid="team-report-row"
      className="flex w-full items-center justify-between gap-3 rounded-atlas-md border border-border bg-[var(--surface-card)] px-[var(--space-4)] py-[var(--space-3)] text-left hover:bg-muted"
    >
      <span className="flex min-w-0 flex-col">
        <span className="truncate atlas-text-body font-medium text-foreground">
          {report.title}
        </span>
        <span className="atlas-text-caption text-muted-foreground">
          {formatMoney(report.total_amount, report.currency)}
          {report.submitted_at
            ? ` · submitted ${report.submitted_at.slice(0, 10)}`
            : ""}
        </span>
      </span>
      <ReportStatusBadge status={report.status} />
    </button>
  );
}

/**
 * Team member drill-down (Product gap 5): summary KPIs + Trips / Reports tabs.
 * Every item opens READ-ONLY — the existing trip panel and report detail hide
 * owner actions from a non-owner (isRecordOwner) and the server refuses them.
 * A member outside the caller's team is a 404 from the server.
 */
export function TeamMemberScreen({ memberId }: { memberId: string }) {
  // QA round: the tab and an opened report live in the URL
  // (?tab=reports&report=<id>) — linkable, and browser Back returns from a
  // report to the member's report list instead of leaving the member page.
  const router = useRouter();
  const pathname = usePathname() ?? `/team/${memberId}`;
  const searchParams = useSearchParams();
  const urlReport = searchParams?.get("report")?.trim() || null;
  const urlTab: Tab =
    urlReport || searchParams?.get("tab") === "reports" ? "reports" : "trips";
  const [tab, setTabState] = React.useState<Tab>(urlTab);
  const [openReportId, setOpenReportState] = React.useState<string | null>(
    urlReport,
  );
  React.useEffect(() => {
    setTabState(urlTab);
    setOpenReportState(urlReport);
  }, [urlTab, urlReport]);
  const hrefFor = (t: Tab, report: string | null): string => {
    const sp = new URLSearchParams();
    if (t === "reports") sp.set("tab", "reports");
    if (report) sp.set("report", report);
    const qs = sp.toString();
    return qs ? `${pathname}?${qs}` : pathname;
  };
  const setTab = (t: Tab) => {
    setTabState(t);
    router.replace(hrefFor(t, null), { scroll: false });
  };
  const setOpenReportId = (id: string | null) => {
    setOpenReportState(id);
    if (id) {
      setTabState("reports");
      router.push(hrefFor("reports", id), { scroll: false });
    } else {
      router.replace(hrefFor("reports", null), { scroll: false });
    }
  };
  const [tripsPage, setTripsPage] = React.useState(1);
  const [reportsPage, setReportsPage] = React.useState(1);
  const { data, isLoading, isError, error, refetch } = useTeamMember(
    memberId,
    tripsPage,
    reportsPage,
  );

  const back = (
    <Link
      href="/team"
      className="atlas-text-caption flex w-fit items-center gap-1 font-medium text-[var(--color-primary-600)] hover:underline"
    >
      <ArrowLeft className="h-4 w-4" aria-hidden="true" />
      Back to team
    </Link>
  );

  if (openReportId) {
    return (
      <ReportDetail
        reportId={openReportId}
        onBack={() => setOpenReportId(null)}
      />
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4">
        {back}
        <LoadingState variant="card" rows={2} />
      </div>
    );
  }

  if (isError || !data) {
    const notFound = isApiError(error) && error.status === 404;
    return (
      <div className="flex flex-col gap-4">
        {back}
        <ErrorState
          heading={notFound ? "Not on your team" : "Couldn’t load this person"}
          subtext={
            notFound
              ? "This person is not one of your direct reports."
              : "Something went wrong. Please try again."
          }
          onRetry={notFound ? undefined : () => refetch()}
        />
      </div>
    );
  }

  const { member, trips, reports } = data;
  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "trips", label: "Trips", count: trips.total },
    { key: "reports", label: "Reports", count: reports.total },
  ];

  return (
    <div className="flex flex-col gap-6" data-testid="team-member-screen">
      {back}
      <header className="flex flex-col gap-1">
        <h1 className="atlas-text-display flex flex-wrap items-center gap-2 text-foreground">
          {member.name}
          {member.status === "inactive" && <InactiveMarker />}
        </h1>
        <p className="atlas-text-body text-muted-foreground">
          {member.department ?? "No department"} · read-only view
        </p>
      </header>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KPICard label="Open trips" value={String(member.open_trips)} />
        <KPICard
          label="Pending approvals"
          value={String(member.pending_approvals)}
        />
        <KPICard
          label="Spend this month"
          value={formatMoney(member.spend_mtd, member.currency)}
        />
        <KPICard
          label="Spend, last 90 days"
          value={formatMoney(member.spend_90d, member.currency)}
        />
      </div>

      <div
        role="tablist"
        aria-label="Member items"
        className="flex gap-1 border-b border-border"
      >
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            id={`team-tab-${t.key}`}
            aria-selected={tab === t.key}
            aria-controls={`team-panel-${t.key}`}
            onClick={() => setTab(t.key)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 atlas-text-body font-medium",
              tab === t.key
                ? "border-[var(--color-primary-600)] text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label} ({t.count})
          </button>
        ))}
      </div>

      {tab === "trips" ? (
        <section
          role="tabpanel"
          id="team-panel-trips"
          aria-labelledby="team-tab-trips"
          className="flex flex-col gap-3"
        >
          {trips.items.length === 0 ? (
            <EmptyState
              icon={Briefcase}
              heading="No trips"
              subtext={`${member.name} has no trips yet.`}
            />
          ) : (
            <ul className="flex flex-col gap-3" aria-label="Trips">
              {trips.items.map((t) => (
                <li key={t.id}>
                  <TeamTripCard trip={t} travelerName={member.name} />
                </li>
              ))}
            </ul>
          )}
          <Pager page={trips} label="Trips" onPage={setTripsPage} />
        </section>
      ) : (
        <section
          role="tabpanel"
          id="team-panel-reports"
          aria-labelledby="team-tab-reports"
          className="flex flex-col gap-3"
        >
          {reports.items.length === 0 ? (
            <EmptyState
              icon={CreditCard}
              heading="No expense reports"
              subtext={`${member.name} has no expense reports yet.`}
            />
          ) : (
            <ul className="flex flex-col gap-2" aria-label="Expense reports">
              {reports.items.map((r) => (
                <li key={r.id}>
                  <ReportRow report={r} onOpen={setOpenReportId} />
                </li>
              ))}
            </ul>
          )}
          <Pager page={reports} label="Reports" onPage={setReportsPage} />
        </section>
      )}
    </div>
  );
}
