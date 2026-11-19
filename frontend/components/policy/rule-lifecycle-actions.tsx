"use client";

import * as React from "react";

import { AtlasButton, Modal, useAtlasToast } from "@/components/atlas";
import { usePolicyRules, useUpdateRuleStatus } from "@/lib/query/policy-hooks";
import { isApiError } from "@/lib/types/api";
import type { PolicyRule, PolicyRuleStatus } from "@/lib/types/policy";

/**
 * Rule lifecycle controls (Issue 5) — Activate / Archive a policy rule.
 *
 * - draft  → "Activate…" and "Archive draft…"
 * - active → "Archive…"
 * - archived → terminal (no controls)
 *
 * Every transition goes through an in-page confirmation dialog that restates
 * what the rule will do (the SERVER's summary, derived from the rule engine).
 * Activation additionally needs an explicit "matches the intended policy"
 * tick. The server re-validates the definition on activation and audits every
 * transition; the client never changes rule semantics.
 */

export type LifecycleRule = Pick<
  PolicyRule,
  "id" | "title" | "summary" | "status" | "rule_type" | "applies_to"
>;

type Pending = "activate" | "archive" | null;

export function RuleLifecycleActions({
  rule,
  intent,
  onChanged,
}: {
  rule: LifecycleRule;
  /** The intent that produced THIS rule, when known (drafted in this session). */
  intent?: string | null;
  onChanged?: (status: PolicyRuleStatus) => void;
}) {
  const { showToast } = useAtlasToast();
  const updateStatus = useUpdateRuleStatus();
  const rulesQuery = usePolicyRules();
  const [pending, setPending] = React.useState<Pending>(null);
  const [confirmed, setConfirmed] = React.useState(false);

  const summary = Array.isArray(rule.summary) ? rule.summary : [];
  const isDraft = rule.status === "draft";

  const conflicts = React.useMemo(
    () =>
      (rulesQuery.data ?? []).filter(
        (r) =>
          r.id !== rule.id &&
          r.status === "active" &&
          r.rule_type === rule.rule_type &&
          r.applies_to === rule.applies_to,
      ),
    [rulesQuery.data, rule.id, rule.rule_type, rule.applies_to],
  );

  function open(next: Pending) {
    setConfirmed(false);
    setPending(next);
  }

  async function run(status: "active" | "archived") {
    try {
      await updateStatus.mutateAsync({ id: rule.id, status });
      showToast({
        variant: "success",
        title:
          status === "active"
            ? "Rule activated"
            : isDraft
              ? "Draft archived"
              : "Rule archived",
      });
      onChanged?.(status);
    } catch (err) {
      showToast({
        variant: "error",
        title: "Couldn’t update the rule",
        description: isApiError(err) ? err.message : "Please try again.",
      });
    } finally {
      setPending(null);
      setConfirmed(false);
    }
  }

  if (rule.status === "archived") {
    return (
      <p className="text-xs text-muted-foreground">
        Archived rules can’t be reactivated.
      </p>
    );
  }

  const ruleRecap = (
    <div className="mt-3 space-y-2 text-sm" data-testid="lifecycle-rule-recap">
      <p className="font-medium text-foreground">{rule.title}</p>
      {summary.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-xs text-foreground">
          {summary.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {/* Only the intent that produced THIS rule; unknown (navigator, older
          drafts) renders no line rather than a wrong one (UAT2-G). */}
      {intent && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="lifecycle-rule-intent"
        >
          You asked: <span className="text-foreground">{intent}</span>
        </p>
      )}
    </div>
  );

  return (
    <div className="flex flex-wrap gap-2">
      {isDraft && (
        <AtlasButton
          variant="primary"
          size="sm"
          onClick={() => open("activate")}
          loading={updateStatus.isPending && pending === "activate"}
        >
          Activate…
        </AtlasButton>
      )}
      <AtlasButton
        variant="destructive"
        size="sm"
        onClick={() => open("archive")}
        loading={updateStatus.isPending && pending === "archive"}
      >
        {isDraft ? "Archive draft…" : "Archive…"}
      </AtlasButton>

      <Modal
        variant="confirm"
        open={pending === "activate"}
        onOpenChange={(o) => {
          if (!o) setPending(null);
        }}
        title="Activate this rule?"
        description="Once active it is enforced on expense pre-checks, approvals and trip planning across your organization. It can’t be returned to draft — only archived."
        confirmLabel="Activate rule"
        confirmDisabled={!confirmed}
        confirmDisabledReason="Confirm that the rule matches the intended policy"
        confirmLoading={updateStatus.isPending}
        contentClassName="sm:max-w-[560px]"
        onConfirm={() => void run("active")}
      >
        {ruleRecap}
        {conflicts.length > 0 && (
          <p
            role="note"
            className="mt-3 rounded-atlas-sm border border-[var(--color-warning)] p-2 text-xs text-foreground"
            data-testid="lifecycle-conflicts"
          >
            Already active for {rule.applies_to.replace(/_/g, " ")}:{" "}
            {conflicts.map((c) => c.title).join("; ")}. Both will be enforced.
          </p>
        )}
        <label className="mt-3 flex items-start gap-2 text-xs text-foreground">
          <input
            id={`confirm-rule-matches-${rule.id}`}
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            I’ve reviewed what this rule will do and it matches the intended
            policy.
          </span>
        </label>
      </Modal>

      <Modal
        variant="destructive"
        open={pending === "archive"}
        onOpenChange={(o) => {
          if (!o) setPending(null);
        }}
        title={isDraft ? "Discard this draft?" : "Archive this active rule?"}
        description={
          isDraft
            ? "The draft is archived and can’t be activated later."
            : "Atlas stops enforcing it immediately. Archived rules can’t be reactivated."
        }
        confirmLabel={isDraft ? "Archive draft" : "Archive rule"}
        confirmLoading={updateStatus.isPending}
        contentClassName="sm:max-w-[560px]"
        onConfirm={() => void run("archived")}
      >
        {ruleRecap}
      </Modal>
    </div>
  );
}
