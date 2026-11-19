"use client";

import * as React from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProvenanceBadge, type ProvenanceType } from "./provenance-badge";
import { ConfidenceIndicator } from "./confidence-indicator";
import { cn } from "@/lib/utils";

/**
 * Atlas Form Field — labeled input with inline validation,
 * helper text, provenance/confidence badges on AI-filled fields.
 *
 * Spec: UI_UX.md → Forms
 * Minimal by design. Provenance/confidence badges on AI-filled fields.
 * Explicit "edited" state after correction.
 */

export interface FormFieldProps {
  /** Unique field id. */
  id: string;
  /** Field label. */
  label: string;
  /** Current value. */
  value: string;
  /** Change handler. */
  onChange?: (value: string) => void;
  /** Input type. Default: "text". */
  type?: string;
  /** Placeholder text. */
  placeholder?: string;
  /** Validation error message. */
  error?: string;
  /** Helper text below the input. */
  helperText?: string;
  /** Field provenance (AI-filled fields). */
  provenance?: ProvenanceType;
  /** Field confidence (AI-filled fields, 0–100). */
  confidence?: number;
  /** Whether the field is required. */
  required?: boolean;
  /** Whether the field is disabled. */
  disabled?: boolean;
  className?: string;
}

export function FormField({
  id,
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  error,
  helperText,
  provenance,
  confidence,
  required = false,
  disabled = false,
  className,
}: FormFieldProps) {
  const [originalValue] = React.useState(value);
  const [hasEdited, setHasEdited] = React.useState(false);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!hasEdited && provenance && provenance !== "user_edited") {
      setHasEdited(true);
    }
    onChange?.(e.target.value);
  };

  const displayProvenance =
    hasEdited && provenance ? "user_edited" : provenance;

  return (
    <div className={cn("space-y-1.5", className)}>
      {/* Label row */}
      <div className="flex items-center gap-2">
        <Label
          htmlFor={id}
          className="atlas-text-body font-medium text-[#334155]"
        >
          {label}
          {required && (
            <span className="text-[var(--color-error)] ml-0.5">*</span>
          )}
        </Label>
        {displayProvenance && <ProvenanceBadge type={displayProvenance} />}
      </div>

      {/* Input */}
      <Input
        id={id}
        type={type}
        value={value}
        onChange={handleChange}
        placeholder={placeholder}
        disabled={disabled}
        required={required}
        className={cn(
          "transition-all duration-atlas-fast",
          error &&
            "border-[var(--color-error)] focus-visible:ring-[var(--color-error)]",
          hasEdited &&
            provenance &&
            "border-[var(--color-success)] focus-visible:ring-[var(--color-success)]",
        )}
        aria-invalid={!!error}
        aria-describedby={
          error ? `${id}-error` : helperText ? `${id}-helper` : undefined
        }
        title={
          hasEdited && provenance ? `Originally: ${originalValue}` : undefined
        }
      />

      {/* Confidence indicator */}
      {confidence !== undefined && (
        <ConfidenceIndicator value={confidence} showLabel />
      )}

      {/* Error message */}
      {error && (
        <p
          id={`${id}-error`}
          className="atlas-text-caption text-[var(--color-error)]"
          role="alert"
        >
          {error}
        </p>
      )}

      {/* Helper text */}
      {helperText && !error && (
        <p id={`${id}-helper`} className="atlas-text-caption text-[#64748B]">
          {helperText}
        </p>
      )}
    </div>
  );
}
