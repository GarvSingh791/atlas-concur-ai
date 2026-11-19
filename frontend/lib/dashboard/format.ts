/**
 * Dashboard display helpers (QA round) — turn the analytics payload's machine
 * keys into readable text. Presentation only: no ids, no business logic.
 */

function humanizeKey(key: string): string {
  const text = key.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

const SUBJECT_LABEL: Record<string, string> = {
  expense_report: "Expense report",
  expense: "Expense",
  trip: "Trip",
  approval: "Approval",
  receipt: "Receipt",
  policy_rule: "Policy rule",
  user: "User",
};

/** "expense_report" -> "Expense report" (any unknown key is humanized). */
export function subjectTypeLabel(subjectType: string): string {
  return SUBJECT_LABEL[subjectType] ?? (humanizeKey(subjectType) || "Item");
}

/**
 * An audit action key as a sentence: "expense_report.withdrawn" ->
 * "Expense report withdrawn", "approval.decided" -> "Approval decided".
 */
export function activityText(action: string): string {
  const [subject, ...rest] = action.split(".");
  const verb = rest.join(" ").replace(/[_-]+/g, " ").trim().toLowerCase();
  const head = subjectTypeLabel(subject ?? "");
  if (!verb) return humanizeKey(action) || "Activity";
  return `${head} ${verb}`;
}

/** "2d ago" / "today" for an age in whole days. */
export function ageText(days: number): string {
  if (!Number.isFinite(days) || days <= 0) return "today";
  return `${days}d ago`;
}
