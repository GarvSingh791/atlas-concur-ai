"use client";

/**
 * Readable display over a rule's structured `definition` JSONB (Issue 5).
 *
 * A presentation of the SERVER's stored form — the client never synthesizes or
 * mutates rule semantics here. Tiers / rates render as readable rows
 * ("Tier 1 (New York, San Francisco, London, …): $300/night"); the raw JSON is
 * always available behind a "Raw definition" disclosure.
 */

type Row = { label: string; lines: string[] };

const KEY_LABELS: Record<string, string> = {
  tiers: "Limits",
  rates: "Daily rates",
  per: "Per",
  currency: "Currency",
  exception_approver: "Exception approver",
  override_approver: "Override approver",
  group_meal_approver: "Group-meal approver",
  group_meal_threshold: "Group meal from",
  expense_categories: "Expense categories",
  preferred_suppliers: "Preferred suppliers",
  within_pct: "Price allowance",
  allowed_classes: "Allowed",
  excluded_classes: "Excluded",
  conditional_classes: "Needs approval",
  threshold_usd: "Receipt from",
  always_required_categories: "Always needs a receipt",
  consequence: "Consequence",
  exclusions: "Exclusions",
  condition: "Condition",
  basis: "Basis",
  corpus_clause_ref: "Policy clause",
  // Product gap 2 / 8 — entry-level rules and ground sub-types.
  kind: "Rule kind",
  ground_subtypes: "Ground transport kinds",
  auto_approve_over_cap_pct: "Auto-approved over cap up to (%)",
  auto_approve_clause_ref: "Tolerance clause",
  auto_approve_requires: "Auto-approval requires",
  partial_day_pct: "Departure / return days (% of rate)",
  partial_day_clause_ref: "Partial-day clause",
  exclusions_clause_ref: "Exclusions clause",
  standard_classes: "Standard classes",
  premium_classes: "Premium classes",
  premium_min_hours: "Premium from (hours)",
  premium_condition: "Premium condition",
  max_days: "Max days",
  lot: "Parking lot",
  max_tip_pct: "Max tip (% of fare)",
};

function humanize(value: unknown): string {
  const text = String(value ?? "")
    .replace(/_/g, " ")
    .trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "—";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function money(value: unknown, currency: unknown): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const code = typeof currency === "string" && currency ? currency : "USD";
  const shown = Number.isInteger(value)
    ? value.toLocaleString("en-US")
    : value.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
  return code === "USD" ? `$${shown}` : `${code} ${shown}`;
}

function places(row: Record<string, unknown>, limit = 3): string | null {
  const cities = Array.isArray(row.cities)
    ? row.cities.filter((c): c is string => typeof c === "string" && !!c)
    : [];
  if (cities.length === 0) return null;
  const shown = cities.slice(0, limit).join(", ");
  return cities.length > limit ? `${shown}, …` : shown;
}

function tierNumber(row: Record<string, unknown>): number {
  const label = String(row.label ?? row.tier ?? "");
  const match = label.match(/(\d+)/);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}

/** "Tier 1 (New York, …): $300/night" / "Tier 3 (all other locations): $160/night". */
function tierLines(
  rows: unknown,
  valueKey: "cap" | "amount",
  currency: unknown,
  unit: string,
): string[] | null {
  if (!Array.isArray(rows)) return null;
  const records = rows.filter(isRecord);
  if (records.length !== rows.length) return null;
  // A lone tier that is unnamed or just "standard" (and not place-scoped) has
  // no meaningful name: "$75 per item", not "Standard: $75/item" (UAT2-G).
  if (records.length === 1) {
    const only = records[0];
    const name = String(only.label ?? only.tier ?? only.region ?? "")
      .trim()
      .toLowerCase();
    const amount = money(only[valueKey], currency);
    if (
      amount !== null &&
      (name === "" || name === "standard") &&
      places(only) === null
    ) {
      const per = unit.replace(/^\//, "");
      return [per ? `${amount} per ${per}` : amount];
    }
  }
  const lines = [...records]
    .sort((a, b) => tierNumber(a) - tierNumber(b))
    .map((row) => {
      const amount = money(row[valueKey], currency);
      if (amount === null) return null;
      const name = String(
        row.label ?? (row.tier ? humanize(row.tier) : null) ?? row.region ?? "",
      );
      const where =
        places(row) ?? (row.default === true ? "all other locations" : null);
      const label = name ? humanize(name) : "Limit";
      return `${label}${where ? ` (${where})` : ""}: ${amount}${unit}`;
    });
  return lines.every((l): l is string => l !== null) ? lines : null;
}

function cabinLines(rows: unknown): string[] | null {
  if (!Array.isArray(rows) || !rows.every(isRecord)) return null;
  const bands = rows.map((row) => {
    const lo = typeof row.min_hours === "number" ? row.min_hours : 0;
    const hi = typeof row.max_hours === "number" ? row.max_hours : null;
    const classes = Array.isArray(row.allowed_classes)
      ? row.allowed_classes.map((c) => String(c).replace(/_/g, " "))
      : [];
    const span =
      hi === null
        ? lo
          ? `${lo}h and over`
          : "any length"
        : lo === 0
          ? `under ${hi}h`
          : `${lo}–${hi}h`;
    return { lo, text: `${span}: ${classes.join(", ") || "none"}` };
  });
  return bands.sort((a, b) => a.lo - b.lo).map((b) => b.text);
}

function listLine(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  if (value.length === 0) return "None";
  if (value.every((v) => typeof v === "string")) {
    return (value as string[]).map((v) => humanize(v)).join(", ");
  }
  if (value.every(isRecord)) {
    return value
      .map((v) =>
        v.class
          ? `${String(v.class)}${v.condition ? ` (${String(v.condition)})` : ""}`
          : JSON.stringify(v),
      )
      .join(", ");
  }
  return null;
}

function rowsFor(definition: Record<string, unknown>): Row[] {
  const currency = definition.currency;
  const per =
    typeof definition.per === "string" && definition.per
      ? `/${definition.per}`
      : "";
  return Object.entries(definition).map(([key, value]) => {
    const label = KEY_LABELS[key] ?? humanize(key);
    let lines: string[] | null = null;
    if (key === "tiers") {
      lines =
        Array.isArray(value) &&
        value.some((t) => isRecord(t) && "allowed_classes" in t)
          ? cabinLines(value)
          : tierLines(value, "cap", currency, per);
    } else if (key === "rates") {
      lines = tierLines(value, "amount", currency, per || "/day");
    } else if (key === "threshold_usd") {
      const amount = money(value, "USD");
      lines = amount ? [`${amount} or more`] : null;
    } else if (key === "within_pct" && typeof value === "number") {
      lines = [`within ${value}%`];
    } else if (key === "group_meal_threshold" && typeof value === "number") {
      lines = [`${value} attendees`];
    } else if (Array.isArray(value)) {
      const line = listLine(value);
      lines = line === null ? null : [line];
    } else if (value === null || value === undefined) {
      lines = ["—"];
    } else if (typeof value !== "object") {
      lines = [
        key.endsWith("approver") || key === "currency" || key.endsWith("_ref")
          ? String(value)
          : humanize(value),
      ];
    }
    return { label, lines: lines ?? [JSON.stringify(value)] };
  });
}

export function RuleDefinition({
  definition,
}: {
  definition: Record<string, unknown>;
}) {
  const record = isRecord(definition) ? definition : {};
  const rows = rowsFor(record);

  if (rows.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">No definition fields.</p>
    );
  }

  return (
    <div className="space-y-2" data-testid="rule-definition">
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs">
        {rows.map(({ label, lines }) => (
          <div key={label} className="contents">
            <dt className="font-medium text-muted-foreground">{label}</dt>
            <dd className="text-foreground break-words">
              {lines.length === 1 ? (
                lines[0]
              ) : (
                <ul className="space-y-0.5">
                  {lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground">
          Raw definition
        </summary>
        <pre
          className="mt-1 max-h-60 overflow-auto rounded-atlas-sm bg-[var(--surface-table-stripe)] p-2 font-mono text-[11px] text-foreground"
          data-testid="rule-definition-raw"
        >
          {JSON.stringify(record, null, 2)}
        </pre>
      </details>
    </div>
  );
}
