import * as React from "react";
import {
  CheckCircle2,
  AlertTriangle,
  AlertOctagon,
  Paperclip,
  Trash2,
} from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AtlasButton, CitationChip } from "@/components/atlas";
import { cn } from "@/lib/utils";
import {
  readinessLabel,
  suggestedFix,
  type ReadinessFlag,
  type ReadinessLevel,
  type ReadinessSummary,
} from "@/lib/expenses/readiness";

/**
 * Pre-submit readiness panel — P5-T7 (US-E4 AC4).
 *
 * Summarizes the normalized pre_submit_flags: a Ready / Needs review / Issue
 * found headline plus each flag's plain-language message, a suggested fix, and a
 * clause citation chip. It renders server-provided flag data only — it never
 * evaluates policy.
 */

const LEVEL_STYLES: Record<
  ReadinessLevel,
  { className: string; Icon: React.ElementType; tone: string }
> = {
  ready: {
    className: "border-border bg-muted",
    Icon: CheckCircle2,
    tone: "text-[var(--color-success)]",
  },
  review: {
    className: "border-border bg-muted",
    Icon: AlertTriangle,
    tone: "text-[var(--color-warning)]",
  },
  issue: {
    className: "border-border bg-muted",
    Icon: AlertOctagon,
    tone: "text-[var(--color-error)]",
  },
};

export interface ReadinessPanelProps {
  readiness: ReadinessSummary;
  /**
   * C7R UAT: open the entry editor for a flagged entry (draft report, owner).
   * `justify` = focus the justification box. Omitted → no actions (read-only).
   */
  onEditEntry?: (entryId: string, opts: { justify: boolean }) => void;
  /**
   * UAT2: evidence fixes for a flagged entry (draft report, owner). A missing /
   * mismatched receipt offers "Attach receipt"; a possible duplicate offers
   * "Delete entry" when the server says the entry is deletable.
   */
  onAttachReceipt?: (entryId: string) => void;
  onDeleteEntry?: (entryId: string) => void;
  /** Entry id → whether the server allows deleting it now. */
  canDeleteEntry?: (entryId: string) => boolean;
}

const RECEIPT_FLAGS = new Set(["missing_receipt", "receipt_mismatch"]);

function FlagAction({
  flag,
  onEditEntry,
  onAttachReceipt,
  onDeleteEntry,
  canDeleteEntry,
}: {
  flag: ReadinessFlag;
  onEditEntry?: ReadinessPanelProps["onEditEntry"];
  onAttachReceipt?: ReadinessPanelProps["onAttachReceipt"];
  onDeleteEntry?: ReadinessPanelProps["onDeleteEntry"];
  canDeleteEntry?: ReadinessPanelProps["canDeleteEntry"];
}) {
  const entryId = flag.entryId;
  if (!entryId) return null;
  const justify =
    flag.flagType === "policy_violation" && flag.justifiable !== false;
  const label = justify
    ? flag.justification
      ? "Edit justification"
      : "Add justification"
    : "Edit entry";
  const attach =
    onAttachReceipt && flag.flagType && RECEIPT_FLAGS.has(flag.flagType);
  const del =
    onDeleteEntry &&
    flag.flagType === "possible_duplicate" &&
    (canDeleteEntry ? canDeleteEntry(entryId) : true);
  if (!onEditEntry && !attach && !del) return null;
  return (
    <span className="mt-1 flex flex-wrap gap-2">
      {attach && (
        <AtlasButton
          variant="secondary"
          size="sm"
          onClick={() => onAttachReceipt(entryId)}
        >
          <Paperclip className="h-4 w-4" aria-hidden="true" />
          Attach receipt
        </AtlasButton>
      )}
      {del && (
        <AtlasButton
          variant="secondary"
          size="sm"
          onClick={() => onDeleteEntry(entryId)}
        >
          <Trash2 className="h-4 w-4" aria-hidden="true" />
          Delete entry
        </AtlasButton>
      )}
      {onEditEntry && (
        <AtlasButton
          variant={attach || del ? "ghost" : "secondary"}
          size="sm"
          onClick={() => onEditEntry(entryId, { justify })}
        >
          {label}
        </AtlasButton>
      )}
    </span>
  );
}

export function ReadinessPanel({
  readiness,
  onEditEntry,
  onAttachReceipt,
  onDeleteEntry,
  canDeleteEntry,
}: ReadinessPanelProps) {
  const { level, flags, issueCount, reviewCount } = readiness;
  const style = LEVEL_STYLES[level];
  const HeadIcon = style.Icon;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-2">
            <HeadIcon
              className={cn("h-5 w-5 shrink-0", style.tone)}
              aria-hidden="true"
            />
            <span className="text-foreground">Pre-submit check</span>
          </span>
          <span className={cn("atlas-text-body font-semibold", style.tone)}>
            {readinessLabel(level)}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {level === "ready" ? (
          <p className="atlas-text-body text-muted-foreground">
            No policy issues found. This report is ready to submit.
          </p>
        ) : (
          <>
            <p className="atlas-text-caption text-muted-foreground">
              {issueCount > 0 && (
                <span className="text-[var(--color-error)]">
                  {issueCount} issue{issueCount === 1 ? "" : "s"}
                </span>
              )}
              {issueCount > 0 && reviewCount > 0 && " · "}
              {reviewCount > 0 && (
                <span className="text-[var(--color-warning)]">
                  {reviewCount} to review
                </span>
              )}
            </p>
            <ul className="flex flex-col gap-3">
              {flags.map((flag, i) => {
                const isIssue = flag.severity === "issue";
                const Icon = isIssue ? AlertOctagon : AlertTriangle;
                const tone = isIssue
                  ? "text-[var(--color-error)]"
                  : "text-[var(--color-warning)]";
                return (
                  <li
                    key={i}
                    className="flex flex-col gap-1.5 rounded-atlas-md border border-border bg-muted p-3"
                  >
                    <div className="flex items-start gap-2">
                      <Icon
                        className={cn("mt-0.5 h-4 w-4 shrink-0", tone)}
                        aria-hidden="true"
                      />
                      <div className="flex min-w-0 flex-col gap-1">
                        <span className="atlas-text-body break-words text-foreground">
                          {flag.message}
                        </span>
                        <span className="atlas-text-caption text-muted-foreground">
                          {suggestedFix(flag)}
                        </span>
                        {flag.justification && (
                          <blockquote className="atlas-text-caption whitespace-pre-wrap break-words border-l-2 border-border pl-2 text-foreground">
                            {flag.justification}
                          </blockquote>
                        )}
                        {flag.clauseRef && (
                          <span className="mt-1">
                            <CitationChip clauseRef={flag.clauseRef} />
                          </span>
                        )}
                        <FlagAction
                          flag={flag}
                          onEditEntry={onEditEntry}
                          onAttachReceipt={onAttachReceipt}
                          onDeleteEntry={onDeleteEntry}
                          canDeleteEntry={canDeleteEntry}
                        />
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
