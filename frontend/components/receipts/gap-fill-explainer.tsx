"use client";

import * as React from "react";
import { AIReasoningPanel } from "@/components/atlas";
import type { GroundingContext } from "@/lib/types";

/**
 * Gap-fill explainer — P5-T6 / US-E3 AC2.
 *
 * The "Why this value?" affordance for an AI-inferred field. It renders ONLY the
 * server's grounding_context (the itinerary/calendar facts the gap-fill agent
 * used) — it never synthesizes a reason on the client.
 *
 * NOTE: ReceiptResponse does not currently serialize grounding_context, so until
 * the backend exposes it this degrades to an honest, clearly-labelled note rather
 * than fabricated specifics. It is forward-compatible: real grounding renders the
 * moment the field is present.
 */

export interface GapFillExplainerProps {
  fieldLabel: string;
  grounding?: GroundingContext | null;
  className?: string;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function formatDay(value: string | null): string | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const [, y, m, d] = match;
  const idx = Number(m) - 1;
  if (idx < 0 || idx > 11) return value;
  return `${MONTHS[idx]} ${Number(d)}, ${y}`;
}

function formatRange(start: string | null, end: string | null): string {
  const s = formatDay(start);
  const e = formatDay(end);
  if (s && e) return `${s} – ${e}`;
  return s ?? e ?? "";
}

function buildSteps(grounding?: GroundingContext | null): string[] {
  const steps: string[] = [];

  for (const seg of grounding?.segments ?? []) {
    const where = seg.location ? ` in ${seg.location}` : "";
    const when = formatRange(seg.start, seg.end);
    steps.push(`Trip ${seg.type}${where}${when ? ` (${when})` : ""}`.trim());
  }

  for (const evt of grounding?.calendar_events ?? []) {
    const where = evt.location ? ` in ${evt.location}` : "";
    const when = formatRange(evt.start, evt.end);
    steps.push(
      `Calendar: ${evt.title}${where}${when ? ` (${when})` : ""}`.trim(),
    );
  }

  return steps;
}

export function GapFillExplainer({
  fieldLabel,
  grounding,
  className,
}: GapFillExplainerProps) {
  const steps = buildSteps(grounding);

  // No grounding facts available from the API → honest, non-fabricated note.
  const displaySteps =
    steps.length > 0
      ? steps
      : [
          `${fieldLabel} was inferred by AI from your trip itinerary and calendar context. Detailed grounding is not available from the receipts API yet.`,
        ];

  return (
    <AIReasoningPanel
      title="Why this value?"
      steps={displaySteps}
      className={className}
    />
  );
}
