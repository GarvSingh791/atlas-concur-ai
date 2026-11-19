"use client";

import * as React from "react";
import { Filter, X } from "lucide-react";

import { AtlasButton } from "@/components/atlas";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** The exact-match filters the audit trail exposes (server allow-listed). */
export interface AuditFilterValues {
  action: string;
  entity_type: string;
}

export const EMPTY_AUDIT_FILTERS: AuditFilterValues = {
  action: "",
  entity_type: "",
};

export interface AuditFiltersProps {
  /** The currently applied filters (drives the draft's initial values). */
  value: AuditFilterValues;
  /** Called with the trimmed draft when the user applies or clears. */
  onApply: (next: AuditFilterValues) => void;
}

/**
 * Audit trail filter bar — exact-match `action` / `entity_type` filters.
 *
 * Filters are applied on submit (not per keystroke) so typing never fires a
 * request per character against the org-wide log.
 */
export function AuditFilters({ value, onApply }: AuditFiltersProps) {
  const [draft, setDraft] = React.useState<AuditFilterValues>(value);

  React.useEffect(() => {
    setDraft(value);
  }, [value]);

  const hasApplied = value.action !== "" || value.entity_type !== "";

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    onApply({
      action: draft.action.trim(),
      entity_type: draft.entity_type.trim(),
    });
  };

  const clear = () => {
    setDraft(EMPTY_AUDIT_FILTERS);
    onApply(EMPTY_AUDIT_FILTERS);
  };

  return (
    <form
      onSubmit={submit}
      aria-label="Filter audit events"
      className="flex flex-col gap-3 sm:flex-row sm:items-end"
    >
      <div className="flex flex-col gap-1.5 sm:w-56">
        <Label htmlFor="audit-filter-action">Action</Label>
        <Input
          id="audit-filter-action"
          value={draft.action}
          onChange={(e) => setDraft((d) => ({ ...d, action: e.target.value }))}
          placeholder="e.g. trip.created"
          autoComplete="off"
        />
      </div>
      <div className="flex flex-col gap-1.5 sm:w-56">
        <Label htmlFor="audit-filter-entity-type">Entity type</Label>
        <Input
          id="audit-filter-entity-type"
          value={draft.entity_type}
          onChange={(e) =>
            setDraft((d) => ({ ...d, entity_type: e.target.value }))
          }
          placeholder="e.g. expense_report"
          autoComplete="off"
        />
      </div>
      <div className="flex items-center gap-2">
        <AtlasButton type="submit" variant="secondary" size="md">
          <Filter className="h-4 w-4" aria-hidden="true" />
          Apply filters
        </AtlasButton>
        {hasApplied && (
          <AtlasButton type="button" variant="ghost" size="md" onClick={clear}>
            <X className="h-4 w-4" aria-hidden="true" />
            Clear
          </AtlasButton>
        )}
      </div>
    </form>
  );
}
