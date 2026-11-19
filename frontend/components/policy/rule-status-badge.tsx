"use client";

import { StatusBadge } from "@/components/atlas";
import type { StatusBadgeVariant } from "@/components/atlas";
import type { PolicyRuleStatus } from "@/lib/types/policy";

const STATUS_VARIANT: Record<PolicyRuleStatus, StatusBadgeVariant> = {
  draft: "rule-draft",
  active: "rule-active",
  archived: "rule-archived",
};

/** Maps a policy_rules.status enum value to the StatusBadge lifecycle dot. */
export function RuleStatusBadge({ status }: { status: PolicyRuleStatus }) {
  return <StatusBadge variant={STATUS_VARIANT[status]} />;
}
