"use client";

import { History } from "lucide-react";

import { AtlasButton, LoadingState } from "@/components/atlas";
import { usePolicyRuleHistory } from "@/lib/query/policy-hooks";
import type { RuleFieldChange } from "@/lib/types/policy";

/**
 * Per-rule change history (product gap 8).
 *
 * Renders the SERVER's audited changes of one rule (GET /policy/rules/{id}/history):
 * when, who (or "System" for a migration / seed), and each changed field's value
 * before → after. Read-only; the client never derives history itself.
 */

const FIELD_LABELS: Record<string, string> = {
  status: "Status",
  title: "Title",
  rule_type: "Rule type",
  applies_to: "Applies to",
  source_clause_ref: "Policy clause",
  definition: "Definition",
};

export function fieldLabel(field: string): string {
  if (FIELD_LABELS[field]) return FIELD_LABELS[field];
  const key = field.startsWith("definition.")
    ? field.slice("definition.".length)
    : field;
  const text = key.replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : field;
}

/** A short, readable rendering of one before / after value. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value || "—";
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  const json = JSON.stringify(value);
  return json.length > 120 ? `${json.slice(0, 119)}…` : json;
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ChangeLine({ change }: { change: RuleFieldChange }) {
  return (
    <li data-testid="rule-history-change" className="text-xs text-foreground">
      <span className="font-medium">{fieldLabel(change.field)}:</span>{" "}
      <span className="text-muted-foreground line-through decoration-1">
        {formatValue(change.before)}
      </span>{" "}
      <span aria-hidden="true">→</span>
      <span className="sr-only">changed to</span>{" "}
      <span>{formatValue(change.after)}</span>
    </li>
  );
}

export function RuleHistory({ ruleId }: { ruleId: string }) {
  const query = usePolicyRuleHistory(ruleId);

  return (
    <div data-testid="rule-history" className="space-y-2">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <History className="h-3.5 w-3.5" aria-hidden="true" />
        Change history
      </p>
      {query.isLoading ? (
        <LoadingState variant="card" />
      ) : query.isError ? (
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>Could not load this rule&apos;s history.</span>
          <AtlasButton
            variant="secondary"
            size="sm"
            onClick={() => query.refetch()}
          >
            Retry
          </AtlasButton>
        </div>
      ) : (query.data ?? []).length === 0 ? (
        <p
          className="text-xs text-muted-foreground"
          data-testid="rule-history-empty"
        >
          No recorded changes yet — this rule has not been edited since it was
          added.
        </p>
      ) : (
        <ol className="space-y-2 border-l border-border pl-3">
          {[...(query.data ?? [])].reverse().map((entry) => (
            <li key={entry.id} data-testid="rule-history-entry">
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 text-xs">
                <span className="font-medium text-foreground">
                  {entry.label}
                </span>
                <time dateTime={entry.at} className="text-muted-foreground">
                  {formatWhen(entry.at)}
                </time>
              </div>
              <p className="text-xs text-muted-foreground">
                by {entry.actor_name ?? "System"}
                {entry.note ? ` · ${entry.note}` : ""}
              </p>
              {entry.changes.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {entry.changes.map((change) => (
                    <ChangeLine key={change.field} change={change} />
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
