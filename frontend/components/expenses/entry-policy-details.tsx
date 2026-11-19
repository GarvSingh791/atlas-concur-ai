import * as React from "react";

import type { ExpenseEntry } from "@/lib/types";
import {
  isMileage,
  showsAttendees,
  showsDisclosure,
} from "@/lib/expenses/policy-fields";

/**
 * Product gap 2 review fix — the policy facts a reviewer is asked to confirm.
 *
 * MIL-01 / MIL-02 / MIL-03, the MEAL-05 / ENT-02 attendee count and the ENT-04
 * government-official case are claimant-entered facts Atlas cannot verify, so the
 * flag sends them to a human. This read-only block renders the STORED facts on
 * every entries table (the approver's decision panel included): the trip (from →
 * to, one-way distance + unit, round trip, commute offset, billable miles × the
 * server's rate), the attendee count and the gift disclosure. Nothing here is
 * computed client-side — every value is the entry DTO's own.
 */

function yesNo(v: boolean | null | undefined): string {
  if (v === true) return "Yes";
  if (v === false) return "No";
  return "Not stated";
}

function orMissing(v: string | null | undefined): string {
  return v && v.trim() ? v : "Not recorded";
}

export function EntryPolicyDetails({ entry }: { entry: ExpenseEntry }) {
  const lines: { key: string; label: string; text: string }[] = [];

  if (isMileage(entry.category)) {
    const t = entry.mileage;
    if (!t) {
      lines.push({ key: "trip", label: "Trip", text: "Trip details missing" });
    } else {
      lines.push({
        key: "trip",
        label: "Trip",
        text: `${orMissing(t.origin)} → ${orMissing(t.destination)}`,
      });
      lines.push({
        key: "distance",
        label: "Distance",
        text:
          `${t.distance} ${t.unit} one way` +
          (t.round_trip ? ", round trip" : "") +
          ` · commute offset ${t.commute_offset} ${t.unit}` +
          (t.air_impractical ? " · air travel marked impractical" : ""),
      });
      lines.push({
        key: "billable",
        label: "Billable",
        text: `${t.billable_miles} mi × $${t.rate_per_mile}/mi (${t.rate_clause})`,
      });
    }
  }

  if (showsAttendees(entry.category) && entry.attendee_count != null) {
    lines.push({
      key: "attendees",
      label: "Attendees",
      text: `${entry.attendee_count} (claimant included)`,
    });
  }

  const d = entry.disclosure;
  if (showsDisclosure(entry.category) && d) {
    const who = d.recipient_organization
      ? `${orMissing(d.recipient)} (${d.recipient_organization})`
      : orMissing(d.recipient);
    lines.push({ key: "recipient", label: "Recipient", text: who });
    lines.push({
      key: "purpose",
      label: "Business purpose",
      text: orMissing(d.business_purpose),
    });
    lines.push({
      key: "official",
      label: "Government official",
      text: yesNo(d.government_official),
    });
  }

  if (lines.length === 0) return null;
  return (
    <dl
      className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 atlas-text-caption"
      data-testid={`entry-policy-details-${entry.id}`}
    >
      {lines.map((l) => (
        <React.Fragment key={l.key}>
          <dt className="text-muted-foreground">{l.label}</dt>
          <dd className="text-foreground" data-testid={`entry-policy-${l.key}`}>
            {l.text}
          </dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
