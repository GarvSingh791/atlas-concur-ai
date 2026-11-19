"use client";

import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Atlas Button — extends shadcn patterns with Atlas-specific variants,
 * sizes, loading state, and disabled-reason tooltip.
 *
 * Spec: UI_UX.md → Component System → Buttons
 */
const atlasButtonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium",
    "rounded-atlas-md text-[14px]",
    "transition-[background-color,box-shadow,transform]",
    "duration-atlas-fast",
    "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)]",
    "disabled:pointer-events-none",
    "[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
    "active:scale-[0.97] active:duration-75",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: [
          "bg-[var(--color-primary-600)] text-white",
          "hover:bg-[var(--color-primary-700)]",
          "active:bg-[#3730A3]",
          "disabled:bg-[#E2E8F0] disabled:text-[#94A3B8]",
        ].join(" "),
        secondary: [
          "bg-white text-[var(--color-primary-600)] border border-[var(--color-primary-600)]",
          "hover:bg-[var(--color-primary-50)]",
          "active:bg-[var(--color-primary-100)]",
          "disabled:bg-[#E2E8F0] disabled:text-[#94A3B8] disabled:border-[#E2E8F0]",
        ].join(" "),
        destructive: [
          "bg-[var(--color-error)] text-white",
          "hover:bg-[#B91C1C]",
          "active:bg-[#991B1B]",
          "disabled:bg-[#E2E8F0] disabled:text-[#94A3B8]",
        ].join(" "),
        ghost: [
          "bg-transparent text-[#475569]",
          "hover:bg-[#F1F5F9]",
          "active:bg-[#E2E8F0]",
          "disabled:bg-transparent disabled:text-[#94A3B8]",
        ].join(" "),
      },
      size: {
        sm: "h-9 px-4 py-2",
        md: "h-10 px-5 py-2.5",
        lg: "h-12 px-6 py-3",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
);

export interface AtlasButtonProps
  extends
    Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "disabled">,
    VariantProps<typeof atlasButtonVariants> {
  /** Render as a child component (Slot pattern). */
  asChild?: boolean;
  /** Show a loading spinner and disable interaction. */
  loading?: boolean;
  /** Disable the button. */
  disabled?: boolean;
  /** Tooltip text shown on hover when disabled (e.g. "Resolve issues to submit"). */
  disabledReason?: string;
}

const AtlasButton = React.forwardRef<HTMLButtonElement, AtlasButtonProps>(
  (
    {
      className,
      variant,
      size,
      asChild = false,
      loading = false,
      disabled = false,
      disabledReason,
      children,
      ...props
    },
    ref,
  ) => {
    const Comp = asChild ? Slot : "button";
    const isDisabled = disabled || loading;

    const button = (
      <Comp
        className={cn(
          atlasButtonVariants({ variant, size, className }),
          loading && "opacity-80",
        )}
        ref={ref}
        disabled={isDisabled}
        aria-disabled={isDisabled}
        aria-busy={loading}
        {...props}
      >
        {loading && <Loader2 className="animate-spin" aria-hidden="true" />}
        {children}
      </Comp>
    );

    // Wrap in tooltip if disabled with a reason
    if (isDisabled && disabledReason) {
      return (
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            {/* Do NOT use asChild here — it triggers compose-refs infinite loop.
                TooltipTrigger renders as a button by default; override to span
                via style so the disabled button can receive pointer events. */}
            <TooltipTrigger
              type="button"
              tabIndex={0}
              style={{
                display: "inline-flex",
                pointerEvents: "auto",
                background: "none",
                border: "none",
                padding: 0,
                margin: 0,
              }}
            >
              {button}
            </TooltipTrigger>
            <TooltipContent>
              <p>{disabledReason}</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      );
    }

    return button;
  },
);
AtlasButton.displayName = "AtlasButton";

export { AtlasButton, atlasButtonVariants };
