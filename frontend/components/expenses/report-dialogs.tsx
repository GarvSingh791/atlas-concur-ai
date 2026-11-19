"use client";

import * as React from "react";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { Input } from "@/components/ui/input";
import { useSession } from "@/lib/auth/session-provider";
import { useCreateReport, useRenameReport } from "@/lib/query/expense-hooks";
import { tripOptionLabels } from "@/lib/expenses/trip-options";
import { useTrips } from "@/lib/query/trip-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import type { ExpenseReport } from "@/lib/types";

/**
 * Report assembly dialogs — C7R UAT.
 *
 * Replaces the disabled "Assemble report" button (dev text "Assembly runs in the
 * AI assistant (P5-T10)"). Mount these only while open: they own their query /
 * mutation hooks, so read-only screens never instantiate them.
 *
 *   CreateReportDialog → POST /expense-reports {title, currency, trip_id?}
 *     (a new, EMPTY draft — entries are moved in from another draft). Only the
 *     viewer's own trips are offered: the server rejects linking anyone else's.
 *   RenameReportDialog → PATCH /expense-reports/{id}/details {title} + If-Match.
 */

const TITLE_MAX = 200;
const DEFAULT_CURRENCY = "USD";

function TitleInput({
  id,
  value,
  onChange,
  error,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  error: string | null;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={id}
        className="atlas-text-body font-medium text-foreground"
      >
        Report title
      </label>
      <Input
        id={id}
        value={value}
        maxLength={TITLE_MAX}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error !== null}
        autoFocus
      />
      {error && (
        <p
          role="alert"
          className="atlas-text-caption text-[var(--color-error)]"
        >
          {error}
        </p>
      )}
    </div>
  );
}

export interface CreateReportDialogProps {
  onOpenChange: (open: boolean) => void;
  onCreated: (report: ExpenseReport) => void;
  /** Extra line under the title (e.g. "then move 2 entries into it"). */
  description?: string;
}

export function CreateReportDialog({
  onOpenChange,
  onCreated,
  description,
}: CreateReportDialogProps) {
  const [title, setTitle] = React.useState("");
  const [tripId, setTripId] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const create = useCreateReport();
  const trips = useTrips();
  const { user } = useSession();
  const { showToast } = useAtlasToast();

  const ownTrips = (trips.data?.items ?? []).filter(
    (t) => !user || t.traveler_id === user.id,
  );
  const tripLabels = tripOptionLabels(ownTrips);

  const handleConfirm = () => {
    const clean = title.trim();
    if (!clean) {
      setError("Give the report a title.");
      return;
    }
    if (create.isPending) return;
    setError(null);
    const trip = ownTrips.find((t) => t.id === tripId);
    create.mutate(
      {
        title: clean,
        currency: trip?.currency ?? DEFAULT_CURRENCY,
        trip_id: trip ? trip.id : null,
      },
      {
        onSuccess: (report) => {
          showToast({
            variant: "success",
            title: "Report created",
            description: `“${report.title}” is ready as a draft.`,
          });
          onOpenChange(false);
          onCreated(report);
        },
        onError: (err) =>
          showToast(expenseErrorToast(err, "Couldn’t create the report")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="New expense report"
      description={
        description ??
        "Creates an empty draft. Move entries into it from another draft report, such as your Receipt Inbox."
      }
      confirmLabel={create.isPending ? "Creating…" : "Create report"}
      onConfirm={handleConfirm}
    >
      <div className="mt-3 flex flex-col gap-3">
        <TitleInput
          id="new-report-title"
          value={title}
          onChange={setTitle}
          error={error}
        />
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="new-report-trip"
            className="atlas-text-body font-medium text-foreground"
          >
            Trip (optional)
          </label>
          <select
            id="new-report-trip"
            value={tripId}
            onChange={(e) => setTripId(e.target.value)}
            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
          >
            <option value="">No trip</option>
            {ownTrips.map((t) => (
              <option key={t.id} value={t.id}>
                {tripLabels.get(t.id)}
              </option>
            ))}
          </select>
        </div>
      </div>
    </Modal>
  );
}

export interface RenameReportDialogProps {
  report: Pick<ExpenseReport, "id" | "title" | "version">;
  onOpenChange: (open: boolean) => void;
}

export function RenameReportDialog({
  report,
  onOpenChange,
}: RenameReportDialogProps) {
  const [title, setTitle] = React.useState(report.title);
  const [error, setError] = React.useState<string | null>(null);
  const rename = useRenameReport();
  const { showToast } = useAtlasToast();

  const handleConfirm = () => {
    const clean = title.trim();
    if (!clean) {
      setError("The title can’t be empty.");
      return;
    }
    if (clean === report.title) {
      onOpenChange(false);
      return;
    }
    if (rename.isPending) return;
    setError(null);
    rename.mutate(
      { id: report.id, version: report.version, title: clean },
      {
        onSuccess: () => {
          showToast({ variant: "success", title: "Report renamed" });
          onOpenChange(false);
        },
        onError: (err) =>
          showToast(expenseErrorToast(err, "Couldn’t rename the report")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="Rename report"
      description="Only draft reports can be renamed."
      confirmLabel={rename.isPending ? "Saving…" : "Save"}
      onConfirm={handleConfirm}
    >
      <div className="mt-3">
        <TitleInput
          id="rename-report-title"
          value={title}
          onChange={setTitle}
          error={error}
        />
      </div>
    </Modal>
  );
}
