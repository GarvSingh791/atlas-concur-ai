import * as React from "react";
import { ArrowLeftRight } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  conversionSourceNote,
  describeConversion,
  type AmountConversion,
} from "@/lib/money/fx";

/**
 * Original + converted amount line — product gap 8.
 *
 * Renders the SERVER's conversion verbatim ("EUR 220.00 ≈ USD 238.70 at 1.085")
 * with the simulated-rate provenance note. Renders nothing when there is no
 * conversion (the amount is already in the base currency).
 */
export function AmountConversionNote({
  conversion,
  compact = false,
  className,
  "data-testid": testId = "amount-conversion",
}: {
  conversion: AmountConversion | null | undefined;
  /** Table cells: the converted figure only, provenance in the tooltip. */
  compact?: boolean;
  className?: string;
  "data-testid"?: string;
}) {
  if (!conversion) return null;
  const text = describeConversion(conversion);
  const note = conversionSourceNote(conversion);
  const unsupported = !conversion.supported;
  return (
    <p
      data-testid={testId}
      data-supported={conversion.supported ? "true" : "false"}
      title={note}
      className={cn(
        "atlas-text-caption flex items-start gap-1",
        unsupported ? "text-[var(--color-warning)]" : "text-muted-foreground",
        className,
      )}
    >
      <ArrowLeftRight className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
      <span>
        {text}
        {!compact && !unsupported && <> · {note}</>}
      </span>
    </p>
  );
}
