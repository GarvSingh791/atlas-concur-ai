import * as React from "react";
import { Sparkles } from "lucide-react";

import { formatMoney } from "@/lib/approvals/format";
import type { AiAnalysisView } from "@/lib/approvals/risk-summary";

/**
 * AI Analysis surface — renders the Approval Agent's structured risk_summary
 * signals on the AI plane (--color-ai-*). The agent is a deterministic router:
 * there is NO generated narrative and NO confidence score (recorded doc-vs-code
 * deviation — code wins). This component renders server truth; it never scores.
 */

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="atlas-text-caption text-muted-foreground">{label}</dt>
      <dd className="atlas-text-body text-foreground">{value}</dd>
    </div>
  );
}

/** "2026-11-09T10:15:00Z" -> a short local date-time; null when unparseable. */
function formatTimestamp(iso: string | null): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return new Date(t).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function AiAnalysis({
  analysis,
  currency,
}: {
  analysis: AiAnalysisView;
  currency: string | null;
}) {
  const verdict = analysis.notEvaluated
    ? "Not evaluated — human review required"
    : analysis.route === "auto_approve"
      ? "Auto-approved by the policy agent"
      : analysis.route === "escalate"
        ? "Escalated for human review"
        : "No agent assessment recorded";
  const reassessedAt = formatTimestamp(analysis.reassessedAt);

  return (
    <section
      aria-label="AI analysis"
      className="rounded-atlas-md border border-[var(--color-primary-100)] bg-atlas-ai-surface p-[var(--space-4)]"
    >
      <div className="flex items-center gap-2 text-atlas-ai-primary">
        <Sparkles className="h-4 w-4 shrink-0" aria-hidden="true" />
        <span className="atlas-text-caption font-semibold uppercase tracking-wide">
          Policy agent
        </span>
      </div>
      <p className="atlas-text-body text-foreground mt-2">{verdict}</p>
      {analysis.reassessed && (
        <p
          className="atlas-text-caption text-muted-foreground mt-1"
          data-testid="ai-analysis-reassessed"
        >
          Re-evaluated after resubmission
          {reassessedAt ? ` on ${reassessedAt}` : ""}
        </p>
      )}
      {analysis.rationale.length > 0 && (
        <ul
          className="mt-2 list-disc pl-4 atlas-text-caption text-foreground"
          data-testid="ai-analysis-rationale"
        >
          {analysis.rationale.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3">
        <Field
          label="In policy"
          value={
            analysis.notEvaluated
              ? "Not evaluated"
              : analysis.inPolicy === null
                ? "—"
                : analysis.inPolicy
                  ? "Yes"
                  : "No"
          }
        />
        <Field label="Blocking flags" value={analysis.blockingCount} />
        <Field
          label="Approver required"
          value={analysis.requiresApprover ? "Yes" : "No"}
        />
        <Field
          label="Subject total"
          value={formatMoney(
            analysis.totalUsd !== null ? String(analysis.totalUsd) : null,
            currency ?? "USD",
          )}
        />
      </dl>
      <p className="atlas-text-caption text-muted-foreground mt-3">
        Deterministic policy evaluation — structured signals only, no generated
        narrative.
      </p>
    </section>
  );
}
