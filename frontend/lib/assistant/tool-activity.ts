/**
 * Tool-activity mapper for the reusable AI Assistant (P5-T10).
 *
 * Maps the REAL persisted grounding trace
 * (`conversation_messages.tool_calls`, shape `{ tools_used: { … } }`) — or a
 * live `tool_activity` SSE frame's `tool_calls` payload, should the backend
 * ever emit one — into renderable per-tool steps.
 *
 * IMPORTANT: this renders only what the trace contains. It NEVER invents tool
 * steps that did not occur. Entries present in the trace ran to completion (the
 * trace is recorded post-hoc), so they map to "complete" unless the entry
 * carries an explicit error/failed marker.
 */

export type ToolStatus = "active" | "complete" | "failed";

export interface ToolStep {
  /** Stable key for React lists (the raw trace key). */
  id: string;
  /** Friendly tool-layer label (Inventory, Policy Evaluation, …). */
  name: string;
  /** Optional sub-qualifier from the raw key (e.g. "flights"). */
  detail?: string;
  status: ToolStatus;
}

/**
 * Friendly names for the real tool layer (UI_UX.md). Matched by case-insensitive
 * prefix against the raw trace key (e.g. "InventoryTool.flights").
 */
const TOOL_LABELS: ReadonlyArray<readonly [RegExp, string]> = [
  [/inventory/i, "Inventory"],
  [/policyeval|policy[_-]?eval|policy[_-]?evaluation/i, "Policy Evaluation"],
  [/policyretrieval|retrieval|rag/i, "Policy Retrieval"],
  [/profile|history/i, "User Profile"],
  [/itinerary|calendar/i, "Itinerary"],
];

function friendlyName(rawKey: string): { name: string; detail?: string } {
  const [base, ...rest] = rawKey.split(".");
  const detail = rest.length > 0 ? rest.join(".") : undefined;
  for (const [pattern, label] of TOOL_LABELS) {
    if (pattern.test(base)) return { name: label, detail };
  }
  // Fallback: strip a trailing "Tool" suffix and humanize.
  const humanized = base.replace(/Tool$/i, "").replace(/[_-]/g, " ").trim();
  return { name: humanized || base, detail };
}

function statusFromValue(value: unknown): ToolStatus | "skip" {
  // Explicit booleans: true = ran (complete), false = not used (skip).
  if (value === true) return "complete";
  if (value === false) return "skip";
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    if (v.error || v.failed || v.status === "failed" || v.status === "error") {
      return "failed";
    }
    if (v.status === "active" || v.status === "running") return "active";
    return "complete";
  }
  // Any other non-empty primitive present in the trace = it ran.
  return value == null ? "skip" : "complete";
}

/**
 * Normalize the polymorphic `tool_calls` payload to the inner tool map.
 * Accepts `{ tools_used: { … } }`, a bare `{ … }`, or an array of `{ name, … }`.
 */
function normalizeTrace(toolCalls: unknown): Record<string, unknown> {
  if (!toolCalls || typeof toolCalls !== "object") return {};

  if (Array.isArray(toolCalls)) {
    const map: Record<string, unknown> = {};
    for (const entry of toolCalls) {
      if (entry && typeof entry === "object") {
        const e = entry as Record<string, unknown>;
        const key =
          typeof e.name === "string"
            ? e.name
            : typeof e.tool === "string"
              ? e.tool
              : null;
        if (key) map[key] = e;
      }
    }
    return map;
  }

  const obj = toolCalls as Record<string, unknown>;
  if (obj.tools_used && typeof obj.tools_used === "object") {
    return obj.tools_used as Record<string, unknown>;
  }
  return obj;
}

/**
 * Map a raw `tool_calls` trace into renderable steps. Returns [] when there is
 * nothing to show (so callers can omit the affordance entirely).
 */
export function mapToolActivity(toolCalls: unknown): ToolStep[] {
  const trace = normalizeTrace(toolCalls);
  const steps: ToolStep[] = [];

  for (const [rawKey, value] of Object.entries(trace)) {
    const status = statusFromValue(value);
    if (status === "skip") continue;
    const { name, detail } = friendlyName(rawKey);
    steps.push({ id: rawKey, name, detail, status });
  }

  return steps;
}

/** Short summary line for the collapsed affordance, e.g. "3 tools used". */
export function summarizeToolActivity(steps: ToolStep[]): string {
  if (steps.length === 0) return "";
  const failed = steps.filter((s) => s.status === "failed").length;
  const noun = steps.length === 1 ? "tool" : "tools";
  if (failed > 0) return `${steps.length} ${noun} used · ${failed} failed`;
  return `${steps.length} ${noun} used`;
}
