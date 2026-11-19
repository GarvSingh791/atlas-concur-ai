"use client";

/**
 * Team hooks (Product gap 5) — wired to backend/app/modules/team/router.py:
 *   - useTeamMembers → GET /team/members            (manager → direct reports)
 *   - useTeamMember  → GET /team/members/{id}        (summary + trips + reports)
 * Read-only. The server decides who is on the team (404 outside it).
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { PaginatedResponse } from "@/lib/types/api";
import type {
  TeamListParams,
  TeamMemberDetail,
  TeamMemberSummary,
} from "@/lib/types/team";

/** Drop blank values so the server never receives an empty filter. */
export function cleanTeamParams(params?: TeamListParams): TeamListParams {
  const out: TeamListParams = {};
  if (!params) return out;
  if (params.page) out.page = params.page;
  if (params.size) out.size = params.size;
  const q = params.q?.trim();
  if (q) out.q = q;
  if (params.sort) out.sort = params.sort;
  return out;
}

export const teamKeys = {
  list: (params?: TeamListParams) =>
    ["team", "members", cleanTeamParams(params)] as const,
  member: (id: string, tripsPage: number, reportsPage: number) =>
    ["team", "member", id, tripsPage, reportsPage] as const,
};

export function useTeamMembers(params?: TeamListParams) {
  const cleaned = cleanTeamParams(params);
  return useQuery({
    queryKey: teamKeys.list(cleaned),
    queryFn: () =>
      api.get<PaginatedResponse<TeamMemberSummary>>("/team/members", {
        params: cleaned as Record<string, string | number | undefined>,
      }),
    placeholderData: keepPreviousData,
  });
}

export const TEAM_DETAIL_PAGE_SIZE = 10;

export function useTeamMember(
  id: string | null,
  tripsPage = 1,
  reportsPage = 1,
) {
  return useQuery({
    queryKey: teamKeys.member(id ?? "", tripsPage, reportsPage),
    queryFn: () =>
      api.get<TeamMemberDetail>(`/team/members/${id}`, {
        params: {
          trips_page: tripsPage,
          reports_page: reportsPage,
          size: TEAM_DETAIL_PAGE_SIZE,
        },
      }),
    enabled: Boolean(id),
    placeholderData: keepPreviousData,
  });
}
