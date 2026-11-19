"use client";

import { useMemo, useState } from "react";
import { Search, ShieldCheck } from "lucide-react";

import { usePolicy, usePolicyRules } from "@/lib/query/policy-hooks";
import { useSession } from "@/lib/auth/session-provider";
import {
  AtlasButton,
  CitationChip,
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/atlas";
import { RuleStatusBadge } from "@/components/policy/rule-status-badge";
import { RuleDefinition } from "@/components/policy/rule-definition";
import { RuleAuthoring } from "@/components/policy/rule-authoring";
import { RuleLifecycleActions } from "@/components/policy/rule-lifecycle-actions";
import { RuleHistory } from "@/components/policy/rule-history";
import { PolicyDocumentView } from "@/components/policy/policy-document";
import type { PolicyRule } from "@/lib/types/policy";

/**
 * Navigator group for a rule: its applies_to, except that an "all" rule scoped
 * to expense categories is filed under those categories (e.g. a ground
 * transport cap under "Ground transport", not "All expenses") (UAT2-G).
 */
function ruleGroup(rule: PolicyRule): { key: string; label: string } {
  const cats = rule.definition?.expense_categories;
  if (rule.applies_to === "all" && Array.isArray(cats)) {
    const names = cats.filter(
      (c): c is string => typeof c === "string" && c.trim() !== "",
    );
    if (names.length > 0) {
      const text = names.map((c) => c.replace(/_/g, " ").trim()).join(" and ");
      return {
        key: `category:${names.join("+")}`,
        label: text.charAt(0).toUpperCase() + text.slice(1).toLowerCase(),
      };
    }
  }
  return {
    key: rule.applies_to,
    label: GROUP_LABELS[rule.applies_to] ?? rule.applies_to,
  };
}

/** Navigator group headings (applies_to → readable). */
const GROUP_LABELS: Record<string, string> = {
  flight: "Flights",
  hotel: "Hotels",
  meals: "Meals",
  car: "Car rentals",
  rail: "Rail",
  all: "All expenses",
};

export default function PolicyPage() {
  const { role } = useSession();
  const policyQuery = usePolicy();
  const rulesQuery = usePolicyRules();

  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);

  // Searchable, then grouped by applies_to for the navigator.
  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = q
      ? rules.filter(
          (r) =>
            (r.title ?? "").toLowerCase().includes(q) ||
            r.rule_type.toLowerCase().includes(q) ||
            r.applies_to.toLowerCase().includes(q) ||
            (r.source_clause_ref ?? "").toLowerCase().includes(q),
        )
      : rules;
    const groups = new Map<string, { label: string; rules: PolicyRule[] }>();
    for (const r of filtered) {
      const { key, label } = ruleGroup(r);
      const group = groups.get(key) ?? { label, rules: [] };
      group.rules.push(r);
      groups.set(key, group);
    }
    return Array.from(groups.entries());
  }, [rules, search]);

  const selected = rules.find((r) => r.id === selectedId) ?? null;

  if (!policyQuery.isLoading && role !== "finance_admin") {
    return (
      <div className="flex h-[calc(100vh-100px)] items-center justify-center">
        <EmptyState
          icon={ShieldCheck}
          heading="Policy & Admin"
          subtext="Policy administration is available to finance administrators."
        />
      </div>
    );
  }

  const policy = policyQuery.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold text-foreground">
          Policy &amp; Admin
        </h1>
        {policy && (
          <span className="text-xs text-muted-foreground">
            {policy.name} · v{policy.version} · effective{" "}
            {policy.effective_from}
          </span>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        {/* Navigator */}
        <aside className="space-y-3">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search rules…"
              aria-label="Search rules"
              className="w-full rounded-atlas-sm border border-border bg-card py-2 pl-8 pr-3 text-sm text-foreground placeholder:text-muted-foreground"
            />
          </div>

          {rulesQuery.isLoading ? (
            <LoadingState variant="card" />
          ) : rulesQuery.isError ? (
            <ErrorState
              heading="Failed to load rules"
              subtext="Could not fetch the policy rule set."
              onRetry={() => rulesQuery.refetch()}
            />
          ) : grouped.length === 0 ? (
            <EmptyState
              icon={ShieldCheck}
              heading="No rules"
              subtext={
                search
                  ? "No rules match your search."
                  : "This policy has no rules yet."
              }
            />
          ) : (
            <nav className="space-y-4">
              {grouped.map(([key, { label, rules: groupRules }]) => (
                <div key={key}>
                  <h2 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {label}
                  </h2>
                  <ul className="space-y-1">
                    {groupRules.map((rule) => (
                      <li key={rule.id}>
                        <button
                          type="button"
                          onClick={() => setSelectedId(rule.id)}
                          className={`flex w-full items-center justify-between gap-2 rounded-atlas-sm border px-2.5 py-2 text-left text-sm ${
                            selectedId === rule.id
                              ? "border-[var(--color-primary-300)] bg-[var(--color-primary-50)]"
                              : "border-border bg-card hover:bg-[var(--surface-table-stripe)]"
                          }`}
                        >
                          <span className="flex min-w-0 flex-col">
                            <span
                              className="truncate text-foreground"
                              title={rule.title || rule.rule_type}
                            >
                              {rule.title || rule.rule_type}
                            </span>
                            {/* QA round: the clause code tells look-alike
                                rules apart (not repeated when the title has it). */}
                            {rule.source_clause_ref &&
                              !(rule.title ?? "").includes(
                                rule.source_clause_ref,
                              ) && (
                                <span
                                  className="font-mono text-[11px] text-muted-foreground"
                                  data-testid={`rule-clause-${rule.id}`}
                                >
                                  {rule.source_clause_ref}
                                </span>
                              )}
                          </span>
                          <RuleStatusBadge status={rule.status} />
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </nav>
          )}
        </aside>

        {/* Detail + authoring + document */}
        <div className="space-y-6">
          {/* Rule detail */}
          {selected ? (
            <section className="rounded-atlas-md border border-border bg-card p-4">
              <div className="mb-3 flex items-center justify-between gap-2">
                <h2
                  className="text-sm font-medium text-foreground"
                  data-testid="rule-detail-title"
                >
                  {selected.title || selected.rule_type}
                </h2>
                <RuleStatusBadge status={selected.status} />
              </div>
              {(selected.summary ?? []).length > 0 && (
                <div className="mb-3">
                  <p className="mb-1 text-xs font-medium text-muted-foreground">
                    What this rule does
                  </p>
                  <ul
                    className="list-disc space-y-1 pl-5 text-xs text-foreground"
                    data-testid="rule-detail-summary"
                  >
                    {selected.summary.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </div>
              )}
              <RuleDefinition definition={selected.definition} />
              {selected.source_clause_ref && (
                <div className="mt-3">
                  <CitationChip clauseRef={selected.source_clause_ref} />
                </div>
              )}
              <div className="mt-4">
                <RuleLifecycleActions rule={selected} />
              </div>
              <div className="mt-4 border-t border-border pt-3">
                <RuleHistory ruleId={selected.id} />
              </div>
            </section>
          ) : (
            <section className="rounded-atlas-md border border-dashed border-border bg-card p-6 text-center">
              <p className="text-sm text-muted-foreground">
                Select a rule from the navigator to view its definition.
              </p>
            </section>
          )}

          {/* AI rule authoring */}
          {policy && <RuleAuthoring policyId={policy.id} />}

          {/* Policy document */}
          <section className="rounded-atlas-md border border-border bg-card p-4">
            <h2 className="mb-2 text-sm font-medium text-foreground">
              Policy document
            </h2>
            {policyQuery.isLoading ? (
              <LoadingState variant="card" />
            ) : policyQuery.isError ? (
              <ErrorState
                heading="Failed to load policy"
                subtext="Could not fetch the active policy document."
                onRetry={() => policyQuery.refetch()}
              />
            ) : policy?.document?.sections.length ? (
              <PolicyDocumentView document={policy.document} />
            ) : policy?.document_text ? (
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-sans text-xs leading-relaxed text-foreground">
                {policy.document_text}
              </pre>
            ) : (
              <p className="text-xs text-muted-foreground">
                No document text for this policy.
              </p>
            )}
          </section>

          {/* Version history */}
          <section className="rounded-atlas-md border border-border bg-card p-4">
            <h2 className="mb-2 text-sm font-medium text-foreground">
              Version history
            </h2>
            {policy ? (
              <div className="flex items-center justify-between text-xs">
                <span className="text-foreground">
                  Current version: v{policy.version}
                </span>
                <span className="text-muted-foreground">
                  Select a rule to see its change history.
                </span>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                Version history will appear here once the policy loads.
              </p>
            )}
          </section>
        </div>
      </div>

      {/* Defence-in-depth retry affordance if both reads fail at once. */}
      {policyQuery.isError && rulesQuery.isError && (
        <div className="flex justify-center">
          <AtlasButton
            variant="secondary"
            size="sm"
            onClick={() => {
              policyQuery.refetch();
              rulesQuery.refetch();
            }}
          >
            Retry
          </AtlasButton>
        </div>
      )}
    </div>
  );
}
