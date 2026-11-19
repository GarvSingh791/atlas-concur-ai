import * as React from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Atlas Stepper — horizontal progress indicator.
 *
 * Spec: UI_UX.md → Steppers
 * Active = filled indigo circle + bold label; Completed = green check;
 * Upcoming = outlined gray; 2px connector. 48px full-width bar.
 */

export interface StepperProps {
  /** Step labels in order. */
  steps: string[];
  /** Current active step (0-indexed). */
  currentStep: number;
  /** Override completed steps (0-indexed). If not provided, all steps before currentStep are completed. */
  completedSteps?: number[];
  className?: string;
}

export function Stepper({
  steps,
  currentStep,
  completedSteps,
  className,
}: StepperProps) {
  const completed = completedSteps
    ? new Set(completedSteps)
    : new Set(Array.from({ length: currentStep }, (_, i) => i));

  return (
    <div
      className={cn("flex items-center w-full h-12", className)}
      role="navigation"
      aria-label="Progress"
    >
      {steps.map((label, index) => {
        const isCompleted = completed.has(index);
        const isActive = index === currentStep;
        const isUpcoming = !isCompleted && !isActive;
        const isLast = index === steps.length - 1;

        return (
          <React.Fragment key={index}>
            {/* Step */}
            <div
              className="flex items-center gap-2 shrink-0"
              aria-current={isActive ? "step" : undefined}
            >
              {/* Circle / check */}
              <div
                className={cn(
                  "flex items-center justify-center rounded-full shrink-0",
                  "h-7 w-7 text-[12px] font-semibold",
                  "transition-all duration-atlas-fast",
                  isCompleted && "bg-[var(--color-success)] text-white",
                  isActive && "bg-[var(--color-primary-400)] text-white",
                  isUpcoming &&
                    "border-2 border-[#CBD5E1] text-[#94A3B8] bg-white",
                )}
              >
                {isCompleted ? (
                  <Check className="h-4 w-4" aria-hidden="true" />
                ) : (
                  <span>{index + 1}</span>
                )}
              </div>

              {/* Label */}
              <span
                className={cn(
                  "text-[length:var(--text-caption-size)] whitespace-nowrap hidden sm:inline",
                  isActive && "font-bold text-[var(--color-primary-900)]",
                  isCompleted && "font-medium text-[var(--color-success)]",
                  isUpcoming && "font-normal text-[#94A3B8]",
                )}
              >
                {label}
              </span>
            </div>

            {/* Connector */}
            {!isLast && (
              <div
                className={cn(
                  "flex-1 h-[2px] mx-2",
                  "transition-colors duration-atlas-fast",
                  isCompleted ? "bg-[var(--color-success)]" : "bg-[#CBD5E1]",
                )}
                aria-hidden="true"
              />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}
