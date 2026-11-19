"use client";

import * as React from "react";
import { Stepper } from "@/components/atlas/stepper";
import { usePlannerStore } from "@/stores/planner-store";
import { useTrip } from "@/lib/query/trip-hooks";
import { cn } from "@/lib/utils";

const STEPS = [
  "Request",
  "Recommendations",
  "Selection",
  "Approval",
  "Confirmation",
];

// Step indices
const STEP_REQUEST = 0;
const STEP_RECOMMENDATIONS = 1;
const STEP_SELECTION = 2;
const STEP_APPROVAL = 3;
const STEP_CONFIRMATION = 4;

interface PlannerStepperProps {
  className?: string;
}

export function PlannerStepper({ className }: PlannerStepperProps) {
  const shortlist = usePlannerStore((s) => s.shortlist);
  const selectedOptionId = usePlannerStore((s) => s.selectedOptionId);
  const selectedTripId = usePlannerStore((s) => s.selectedTripId);

  const { data: trip } = useTrip(selectedTripId);

  // Derive currentStep from real state — never faked
  let currentStep: number;

  if (trip) {
    const status = trip.status;
    if (status === "approved" || status === "completed") {
      currentStep = STEP_CONFIRMATION;
    } else if (status === "rejected" || status === "cancelled") {
      // Show at Selection step if rejected so user can re-select
      currentStep = STEP_SELECTION;
    } else if (
      status === "pending_approval" ||
      trip.approval_status === "changes_requested"
    ) {
      // Submitted, or returned by the approver for changes (C7R: a returned
      // trip is a draft again but is still in the approval loop).
      currentStep = STEP_APPROVAL;
    } else {
      // draft: saved from the selection but not yet submitted for approval.
      currentStep = STEP_SELECTION;
    }
  } else if (selectedOptionId) {
    // Option selected but trip not yet persisted (in-flight)
    currentStep = STEP_SELECTION;
  } else if (shortlist.length > 0) {
    currentStep = STEP_RECOMMENDATIONS;
  } else {
    currentStep = STEP_REQUEST;
  }

  return (
    <Stepper
      steps={STEPS}
      currentStep={currentStep}
      className={cn("px-[var(--space-4)]", className)}
    />
  );
}
