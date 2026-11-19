"use client";

import * as React from "react";
import { Check, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ChipMeta } from "@/lib/planner/intent";
import type { PlannerIntent } from "@/lib/types";

interface ChipEditorProps {
  chipMeta: ChipMeta;
  currentValue: PlannerIntent[keyof PlannerIntent];
  onSave: (value: string | number | null) => void;
  onCancel: () => void;
}

export function ChipEditor({
  chipMeta,
  currentValue,
  onSave,
  onCancel,
}: ChipEditorProps) {
  const isDate =
    chipMeta.key === "departure_date" || chipMeta.key === "return_date";
  const isNumber = chipMeta.key === "travelers";

  const [draft, setDraft] = React.useState(
    currentValue !== null && currentValue !== undefined
      ? String(currentValue)
      : "",
  );

  const inputRef = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSave = () => {
    if (!draft.trim()) {
      onSave(null);
    } else if (isNumber) {
      const num = parseInt(draft, 10);
      onSave(isNaN(num) || num < 1 ? null : num);
    } else {
      onSave(draft.trim());
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleSave();
    }
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  };

  return (
    <div className="flex items-center gap-1.5 bg-white border border-[#E2E8F0] rounded-atlas-md px-2 py-1 shadow-atlas-2">
      <label className="text-[length:var(--text-caption-size)] font-medium text-[#64748B] shrink-0">
        {chipMeta.label}:
      </label>
      <input
        ref={inputRef}
        type={isDate ? "date" : isNumber ? "number" : "text"}
        min={isNumber ? 1 : undefined}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        className={cn(
          "w-32 text-[length:var(--text-body-size)] border-none outline-none bg-transparent",
          "focus:ring-0",
        )}
        aria-label={`Edit ${chipMeta.label}`}
      />
      <button
        type="button"
        onClick={handleSave}
        aria-label="Save"
        className="text-[var(--color-success)] hover:opacity-70"
      >
        <Check className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={onCancel}
        aria-label="Cancel"
        className="text-[#94A3B8] hover:opacity-70"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
