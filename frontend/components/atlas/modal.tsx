"use client";

import * as React from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AtlasButton } from "./button";
import { cn } from "@/lib/utils";

/**
 * Atlas Modal — confirm / state-change / reason-capture.
 *
 * Spec: UI_UX.md → Modals
 * Always dismissible, clear primary/secondary pair, no nested modals.
 * Elevation level 3, radius --radius-xl.
 */

export type ModalVariant = "confirm" | "destructive" | "reason";

export interface ModalProps {
  /** Modal variant determines button styling. */
  variant?: ModalVariant;
  /** Modal title. */
  title: string;
  /** Modal description/body. */
  description: string;
  /** Confirm button label. Default varies by variant. */
  confirmLabel?: string;
  /** Cancel button label. Default: "Cancel". */
  cancelLabel?: string;
  /** Confirm handler. For "reason" variant, receives the reason text. */
  onConfirm?: (reason?: string) => void;
  /** Cancel handler. */
  onCancel?: () => void;
  /** Controlled open state. */
  open: boolean;
  /** Open change handler. */
  onOpenChange: (open: boolean) => void;
  /** For "reason" variant: whether reason is required. Default: true. */
  requireReason?: boolean;
  /** Children (extra content in the modal body). */
  children?: React.ReactNode;
  /** Disable the confirm button (e.g. until a required checkbox is ticked). */
  confirmDisabled?: boolean;
  /** Tooltip explaining why confirm is disabled. */
  confirmDisabledReason?: string;
  /** Show a spinner on the confirm button while the action runs. */
  confirmLoading?: boolean;
  /** Extra classes for the dialog content (e.g. a wider max-width). */
  contentClassName?: string;
}

const DEFAULT_LABELS: Record<ModalVariant, string> = {
  confirm: "Confirm",
  destructive: "Delete",
  reason: "Submit",
};

export function Modal({
  variant = "confirm",
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
  open,
  onOpenChange,
  requireReason = true,
  children,
  confirmDisabled = false,
  confirmDisabledReason,
  confirmLoading = false,
  contentClassName,
}: ModalProps) {
  const [reason, setReason] = React.useState("");
  const label = confirmLabel ?? DEFAULT_LABELS[variant];
  const isReasonVariant = variant === "reason";
  const canConfirm =
    isReasonVariant && requireReason ? reason.trim().length > 0 : true;

  const handleConfirm = () => {
    onConfirm?.(isReasonVariant ? reason : undefined);
    setReason("");
  };

  const handleCancel = () => {
    onCancel?.();
    onOpenChange(false);
    setReason("");
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          "rounded-atlas-xl shadow-atlas-3 p-[var(--space-6)]",
          "sm:max-w-[425px]",
          contentClassName,
        )}
        onEscapeKeyDown={() => onOpenChange(false)}
      >
        <DialogHeader>
          <DialogTitle className="atlas-text-title text-[var(--color-primary-900)]">
            {title}
          </DialogTitle>
          <DialogDescription className="atlas-text-body text-[#64748B] mt-2">
            {description}
          </DialogDescription>
        </DialogHeader>

        {children}

        {/* Reason text area */}
        {isReasonVariant && (
          <div className="mt-3">
            <label
              htmlFor="modal-reason"
              className="atlas-text-body font-medium text-[#334155] block mb-1.5"
            >
              Reason{" "}
              {requireReason && (
                <span className="text-[var(--color-error)]">*</span>
              )}
            </label>
            <textarea
              id="modal-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Enter your reason..."
              rows={3}
              className={cn(
                "w-full rounded-atlas-md border border-[hsl(var(--input))] bg-[hsl(var(--background))]",
                "px-3 py-2 atlas-text-body",
                "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)]",
                "transition-colors duration-atlas-fast",
                "placeholder:text-[hsl(var(--muted-foreground))]",
              )}
              aria-required={requireReason}
            />
          </div>
        )}

        <DialogFooter className="mt-4">
          <AtlasButton variant="ghost" size="md" onClick={handleCancel}>
            {cancelLabel}
          </AtlasButton>
          <AtlasButton
            variant={variant === "destructive" ? "destructive" : "primary"}
            size="md"
            onClick={handleConfirm}
            disabled={!canConfirm || confirmDisabled}
            loading={confirmLoading}
            disabledReason={
              !canConfirm
                ? "Please enter a reason"
                : confirmDisabled
                  ? confirmDisabledReason
                  : undefined
            }
          >
            {label}
          </AtlasButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
