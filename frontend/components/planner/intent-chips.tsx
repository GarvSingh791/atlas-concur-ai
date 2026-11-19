"use client";

import * as React from "react";
import { Pencil, X, Plus } from "lucide-react";
import { ChipEditor } from "./chip-editor";
import { CHIP_METADATA, missingRequiredSlot } from "@/lib/planner/intent";
import { usePlannerStore } from "@/stores/planner-store";
import type { PlannerIntent } from "@/lib/types";
import { cn } from "@/lib/utils";

const COLOR_CLASSES: Record<
  string,
  { bg: string; text: string; border: string }
> = {
  blue: {
    bg: "bg-[#DBEAFE]",
    text: "text-[#1E40AF]",
    border: "border-[#BFDBFE]",
  },
  green: {
    bg: "bg-[#DCFCE7]",
    text: "text-[#166534]",
    border: "border-[#BBF7D0]",
  },
  amber: {
    bg: "bg-[#FEF3C7]",
    text: "text-[#92400E]",
    border: "border-[#FDE68A]",
  },
  gray: {
    bg: "bg-[#F1F5F9]",
    text: "text-[#475569]",
    border: "border-[#E2E8F0]",
  },
};

interface IntentChipsProps {
  onIntentChange: () => void;
  className?: string;
}

export function IntentChips({ onIntentChange, className }: IntentChipsProps) {
  const intent = usePlannerStore((s) => s.intent);
  const setIntent = usePlannerStore((s) => s.setIntent);
  const streaming = usePlannerStore((s) => s.streaming);
  const [editingKey, setEditingKey] = React.useState<
    keyof PlannerIntent | null
  >(null);

  const missingSlot = missingRequiredSlot(intent);

  const handleSave = (
    key: keyof PlannerIntent,
    value: string | number | null,
  ) => {
    setIntent({ [key]: value } as Partial<PlannerIntent>);
    setEditingKey(null);
    onIntentChange();
  };

  const handleRemove = (key: keyof PlannerIntent) => {
    setIntent({ [key]: null } as Partial<PlannerIntent>);
    onIntentChange();
  };

  return (
    <div
      className={cn(
        "flex flex-wrap gap-2 px-[var(--space-4)] py-[var(--space-3)]",
        className,
      )}
    >
      {CHIP_METADATA.map((meta) => {
        const rawVal = intent[meta.key];
        const hasValue =
          rawVal !== null && rawVal !== undefined && String(rawVal) !== "";
        const isMissing = missingSlot === meta.key;
        const colors = COLOR_CLASSES[meta.color] ?? COLOR_CLASSES.gray;

        if (editingKey === meta.key) {
          return (
            <ChipEditor
              key={meta.key}
              chipMeta={meta}
              currentValue={rawVal}
              onSave={(v) => handleSave(meta.key, v)}
              onCancel={() => setEditingKey(null)}
            />
          );
        }

        if (!hasValue) {
          // Optional chips (Return, Purpose) stay addable: a return date is
          // what sizes the hotel stay (check-in = depart, check-out = return).
          return (
            <button
              key={meta.key}
              type="button"
              disabled={streaming}
              onClick={() => setEditingKey(meta.key)}
              className={cn(
                "flex items-center gap-1 px-3 py-1 rounded-atlas-full border text-[length:var(--text-caption-size)] font-medium",
                "transition-colors",
                isMissing
                  ? "border-[var(--color-warning)] bg-[#FFFBEB] text-[var(--color-warning)] ring-1 ring-[var(--color-warning)]"
                  : "border-dashed border-[#CBD5E1] text-[#94A3B8] hover:border-[#94A3B8]",
                "disabled:opacity-50 disabled:cursor-not-allowed",
              )}
              aria-label={`Add ${meta.label}`}
            >
              <Plus className="h-3 w-3" aria-hidden="true" />
              {meta.label}
            </button>
          );
        }

        return (
          <div
            key={meta.key}
            className={cn(
              "group flex items-center gap-1.5 px-3 py-1 rounded-atlas-full border",
              "text-[length:var(--text-caption-size)] font-medium",
              "transition-all duration-atlas-fast",
              colors.bg,
              colors.text,
              colors.border,
              isMissing && "ring-2 ring-offset-1 ring-[var(--color-warning)]",
            )}
          >
            <span className="text-[10px] font-semibold opacity-60 uppercase tracking-wide">
              {meta.label}
            </span>
            <span>{meta.format(rawVal)}</span>
            <button
              type="button"
              disabled={streaming}
              onClick={() => setEditingKey(meta.key)}
              className="opacity-0 group-hover:opacity-100 transition-opacity hover:opacity-80 disabled:cursor-not-allowed"
              aria-label={`Edit ${meta.label}`}
            >
              <Pencil className="h-3 w-3" aria-hidden="true" />
            </button>
            {!meta.required && (
              <button
                type="button"
                disabled={streaming}
                onClick={() => handleRemove(meta.key)}
                className="opacity-0 group-hover:opacity-100 transition-opacity hover:opacity-80 disabled:cursor-not-allowed"
                aria-label={`Remove ${meta.label}`}
              >
                <X className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
