"use client";

import * as React from "react";
import { Leaf } from "lucide-react";
import { StatusBadge } from "@/components/atlas/status-badge";
import { CitationChip } from "@/components/atlas/citation-chip";
import { AIReasoningPanel } from "@/components/atlas/ai-reasoning-panel";
import { AtlasButton } from "@/components/atlas/button";
import type { ShortlistOption } from "@/lib/types";
import { cn } from "@/lib/utils";

interface OptionCardProps {
  option: ShortlistOption;
  isSelected: boolean;
  onSelect: (optionId: string) => void;
  disabled?: boolean;
  className?: string;
}

function SustainabilityIndicator({ score }: { score: number }) {
  const color =
    score <= 2
      ? "text-[var(--color-success)]"
      : score === 3
        ? "text-[var(--color-warning)]"
        : "text-[#94A3B8]";
  return (
    <span
      className={cn(
        "flex items-center gap-0.5 text-[length:var(--text-caption-size)]",
        color,
      )}
    >
      <Leaf className="h-3 w-3" aria-hidden="true" />
      <span>
        Eco {score}/5{score <= 2 ? " ✓" : ""}
      </span>
    </span>
  );
}

export function OptionCard({
  option,
  isSelected,
  onSelect,
  disabled,
  className,
}: OptionCardProps) {
  const isFlight = option.segment_type === "flight";

  const title = isFlight
    ? `${option.supplier} · ${option.cabin_class ?? "Economy"} · ${option.is_direct ? "Direct" : "Connecting"}`
    : `${option.supplier} · ${option.city ?? ""}`;

  const details = isFlight
    ? [
        option.duration_hours !== null
          ? `${option.duration_hours}h flight`
          : null,
        option.cabin_class,
      ]
        .filter(Boolean)
        .join(" · ")
    : [option.city, option.room_type].filter(Boolean).join(" · ");

  // Hotels: amount_usd is the NIGHTLY rate. Show "$X/night", plus the stay
  // total when the server sent a valid stay length (older shortlists did not).
  const nightlyRate = option.nightly_rate_usd ?? option.amount_usd;
  const stayNights =
    typeof option.nights === "number" && option.nights >= 1
      ? option.nights
      : null;
  const stayTotal =
    stayNights !== null &&
    typeof option.stay_total_usd === "number" &&
    Number.isFinite(option.stay_total_usd)
      ? option.stay_total_usd
      : null;

  return (
    <div
      className={cn(
        "bg-white rounded-atlas-md border shadow-atlas-1 p-[var(--space-4)] transition-all duration-atlas-fast",
        isSelected &&
          "border-[var(--color-primary-400)] ring-2 ring-[var(--color-primary-200)]",
        !isSelected && "border-[#E2E8F0] hover:shadow-atlas-2",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <p className="text-[length:var(--text-subtitle-size)] font-medium text-[#1E293B] truncate">
            {title}
          </p>
          {details && (
            <p className="text-[length:var(--text-caption-size)] text-[#64748B] mt-0.5">
              {details}
            </p>
          )}
        </div>
        <div className="shrink-0 text-right">
          {isFlight ? (
            <p className="text-[length:var(--text-title-size)] font-semibold text-[#1E293B]">
              ${option.amount_usd.toFixed(0)}
            </p>
          ) : (
            <>
              <p className="text-[length:var(--text-title-size)] font-semibold text-[#1E293B]">
                ${nightlyRate.toFixed(0)}/night
              </p>
              {stayTotal !== null && stayNights !== null && (
                <p className="text-[length:var(--text-caption-size)] text-[#64748B]">
                  ${stayTotal.toFixed(0)} total for {stayNights}{" "}
                  {stayNights === 1 ? "night" : "nights"}
                </p>
              )}
            </>
          )}
        </div>
      </div>

      <div className="mt-3 flex items-start gap-2 flex-wrap">
        <StatusBadge
          variant={option.in_policy ? "in-policy" : "out-of-policy"}
        />
        {option.requires_approver && (
          <span className="text-[length:var(--text-caption-size)] bg-[#FFFBEB] text-[var(--color-warning)] border border-[#FDE68A] px-2 py-0.5 rounded-atlas-sm">
            Requires approver
          </span>
        )}
      </div>

      {option.policy_reason && (
        <p className="mt-1.5 text-[length:var(--text-caption-size)] text-[#64748B] italic">
          {option.policy_reason}
        </p>
      )}

      {(option.policy_notices ?? []).length > 0 && (
        <ul className="mt-2 flex flex-col gap-1" aria-label="Policy notices">
          {(option.policy_notices ?? []).map((n, i) => (
            <li
              key={`${n.clause_ref}-${i}`}
              data-testid="option-policy-notice"
              className={cn(
                "text-[length:var(--text-caption-size)] border px-2 py-1 rounded-atlas-sm break-words",
                n.blocking
                  ? "bg-[#FEF2F2] text-[var(--color-error)] border-[#FECACA]"
                  : "bg-[#FFFBEB] text-[#92400E] border-[#FDE68A]",
              )}
            >
              <span className="font-medium">{n.clause_ref}:</span> {n.message}
            </li>
          ))}
        </ul>
      )}

      {option.explanation && (
        <p className="mt-2 text-[length:var(--text-body-size)] text-[#334155]">
          {option.explanation}
        </p>
      )}

      <div className="mt-3 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <SustainabilityIndicator score={option.sustainability_score} />
          {option.clause_refs.map((ref, i) => (
            <CitationChip key={i} clauseRef={ref} />
          ))}
        </div>
        <AtlasButton
          variant={isSelected ? "secondary" : "primary"}
          size="sm"
          onClick={() => !isSelected && onSelect(option.option_id)}
          disabled={disabled || isSelected}
          aria-label={isSelected ? "Selected" : `Select ${title}`}
        >
          {isSelected ? "Selected ✓" : "Select →"}
        </AtlasButton>
      </div>

      {option.explanation && (
        <AIReasoningPanel
          title="Why this recommendation?"
          steps={[option.explanation]}
          className="mt-3"
        />
      )}
    </div>
  );
}
