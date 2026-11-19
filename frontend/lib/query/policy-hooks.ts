"use client";

/**
 * Policy & rule-authoring hooks — P5-T9.
 *
 * Wired to the REAL contract (backend/app/modules/policy/router.py):
 *   - usePolicy           → GET   /policies                  (active doc, org-scoped)
 *   - usePolicyRules      → GET   /policy-rules              (structured rule list)
 *   - useAuthorRule       → POST  /policies/{id}/rules       { title, ... } → DRAFT
 *   - useUpdateRuleStatus → PATCH /policy-rules/{id}         { status } → active|archived
 *   - usePolicyRuleHistory → GET  /policy/rules/{id}/history (audited changes, gap 8)
 *
 * The screen renders server truth. The NL→structured-rule translation is the
 * Policy Agent's job (server-side); the client only submits NL text and renders
 * the SERVER's structured draft. Authored rules are DRAFT — never auto-activated.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type {
  AuthorRuleRequest,
  AuthorRuleResponse,
  Policy,
  PolicyRule,
  RuleHistoryEntry,
  RuleHistoryResponse,
  UpdateRuleStatusRequest,
} from "@/lib/types/policy";

export interface PolicyRuleListParams {
  status?: string;
  applies_to?: string;
}

export const policyKeys = {
  policy: ["policy", "active"] as const,
  rules: (params?: PolicyRuleListParams) =>
    ["policy-rules", params ?? {}] as const,
  history: (ruleId: string) => ["policy-rules", "history", ruleId] as const,
};

export function usePolicy() {
  return useQuery<Policy>({
    queryKey: policyKeys.policy,
    queryFn: () => api.get<Policy>("/policies"),
  });
}

export function usePolicyRules(params?: PolicyRuleListParams) {
  return useQuery<PolicyRule[]>({
    queryKey: policyKeys.rules(params),
    queryFn: () =>
      api.get<PolicyRule[]>("/policy-rules", {
        params: params as Record<string, string | undefined>,
      }),
  });
}

export interface AuthorRuleArgs {
  policyId: string;
  body: AuthorRuleRequest;
}

export function useAuthorRule() {
  const qc = useQueryClient();
  return useMutation<AuthorRuleResponse, unknown, AuthorRuleArgs>({
    mutationFn: ({ policyId, body }) =>
      api.post<AuthorRuleResponse>(`/policies/${policyId}/rules`, { body }),
    onSuccess: () => {
      // A new draft now exists — refresh the rule list so it appears.
      qc.invalidateQueries({ queryKey: ["policy-rules"] });
    },
  });
}

export interface UpdateRuleStatusArgs {
  id: string;
  status: UpdateRuleStatusRequest["status"];
}

/** Keep only well-formed history entries (the server is the source of truth). */
export function normalizeRuleHistory(raw: unknown): RuleHistoryEntry[] {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return [];
  const entries = (raw as Partial<RuleHistoryResponse>).entries;
  if (!Array.isArray(entries)) return [];
  return entries.filter(
    (e): e is RuleHistoryEntry =>
      typeof e === "object" &&
      e !== null &&
      typeof e.id === "string" &&
      typeof e.at === "string" &&
      typeof e.label === "string" &&
      Array.isArray(e.changes),
  );
}

export function usePolicyRuleHistory(ruleId: string | null) {
  return useQuery<RuleHistoryEntry[]>({
    queryKey: policyKeys.history(ruleId ?? ""),
    enabled: !!ruleId,
    queryFn: async () =>
      normalizeRuleHistory(
        await api.get<RuleHistoryResponse>(`/policy/rules/${ruleId}/history`),
      ),
  });
}

export function useUpdateRuleStatus() {
  const qc = useQueryClient();
  return useMutation<PolicyRule, unknown, UpdateRuleStatusArgs>({
    mutationFn: ({ id, status }) =>
      api.patch<PolicyRule>(`/policy-rules/${id}`, { body: { status } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["policy-rules"] });
    },
  });
}
