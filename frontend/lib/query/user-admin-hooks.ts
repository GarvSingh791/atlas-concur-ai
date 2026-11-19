"use client";

/**
 * User administration hooks — product gap 3 (finance_admin only).
 *
 * Wired to backend/app/modules/identity/router.py:
 *   useAdminUsers     → GET   /users?q=&role=&status=&page=&size=
 *   useManagerOptions → GET   /users?status=active&size=100&sort=name (filtered
 *                       to managers / finance admins — the roles a manager may have)
 *   useCreateUser     → POST  /users
 *   useUpdateUser     → PATCH /users/{id}
 *   useSetUserActive  → POST  /users/{id}/deactivate | /reactivate
 *   useGrades         → GET   /grades (traveller grade ladder, AIR-04; static)
 *   useOrganizationLoginCode → GET /organization (the org's login code, read-only)
 *   useInviteUser     → POST  /users/invite (phase 12 follow-ups)
 *   useResendInvite   → POST  /users/{id}/invite/resend
 *   useRevokeInvite   → POST  /users/{id}/invite/revoke
 *
 * Optimistic-free: every mutation invalidates the user queries and the table
 * re-renders from the server's answer. Errors surface the server envelope
 * message (ApiError.message) inline in the dialogs.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { PaginatedResponse } from "@/lib/types/api";
import type { OrganizationResponse } from "@/lib/types/identity";
import type {
  AdminUser,
  GradeOption,
  InviteResponse,
  UserCreateRequest,
  UserInviteRequest,
  UserListParams,
  UserUpdateRequest,
} from "@/lib/types/shell";

export const userAdminKeys = {
  all: ["admin-users"] as const,
  list: (params: UserListParams) => ["admin-users", "list", params] as const,
  managers: () => ["admin-users", "managers"] as const,
  grades: () => ["admin-users", "grades"] as const,
  organization: () => ["admin-users", "organization"] as const,
};

/** The admin's own organisation and its login code (shared with users so they
 * can sign in when their email also exists in another organisation). */
export function useOrganizationLoginCode(enabled = true) {
  return useQuery({
    enabled,
    queryKey: userAdminKeys.organization(),
    queryFn: () => api.get<OrganizationResponse>("/organization"),
    staleTime: 5 * 60 * 1000,
  });
}

function cleanParams(params: UserListParams): UserListParams {
  const out: UserListParams = {};
  if (params.q && params.q.trim()) out.q = params.q.trim();
  if (params.role) out.role = params.role;
  if (params.status) out.status = params.status;
  if (params.page) out.page = params.page;
  if (params.size) out.size = params.size;
  return out;
}

export function useAdminUsers(params: UserListParams, enabled = true) {
  const cleaned = cleanParams(params);
  return useQuery({
    enabled,
    queryKey: userAdminKeys.list(cleaned),
    queryFn: () =>
      api.get<PaginatedResponse<AdminUser>>("/users", {
        params: { sort: "name:asc", ...cleaned } as Record<
          string,
          string | number | undefined
        >,
      }),
    placeholderData: keepPreviousData,
  });
}

export function useManagerOptions() {
  return useQuery({
    queryKey: userAdminKeys.managers(),
    queryFn: () =>
      api.get<PaginatedResponse<AdminUser>>("/users", {
        params: { status: "active", size: 100, sort: "name:asc" },
      }),
    select: (page) =>
      page.items.filter(
        (u) => u.role === "manager" || u.role === "finance_admin",
      ),
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation<AdminUser, unknown, UserCreateRequest>({
    mutationFn: (body) => api.post<AdminUser>("/users", { body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation<
    AdminUser,
    unknown,
    { id: string; changes: UserUpdateRequest }
  >({
    mutationFn: ({ id, changes }) =>
      api.patch<AdminUser>(`/users/${id}`, { body: changes }),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}

export function useSetUserActive() {
  const qc = useQueryClient();
  return useMutation<AdminUser, unknown, { id: string; active: boolean }>({
    mutationFn: ({ id, active }) =>
      api.post<AdminUser>(
        `/users/${id}/${active ? "reactivate" : "deactivate"}`,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}

/** The grade ladder never changes at runtime (a change needs a migration). */
export function useGrades(enabled = true) {
  return useQuery({
    enabled,
    queryKey: userAdminKeys.grades(),
    queryFn: () => api.get<GradeOption[]>("/grades"),
    staleTime: Infinity,
  });
}

// ---------------------------------------------------------------------------
// Invitations (phase 12 follow-ups)
// ---------------------------------------------------------------------------

export function useInviteUser() {
  const qc = useQueryClient();
  return useMutation<InviteResponse, unknown, UserInviteRequest>({
    mutationFn: (body) => api.post<InviteResponse>("/users/invite", { body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}

export function useResendInvite() {
  const qc = useQueryClient();
  return useMutation<InviteResponse, unknown, { id: string }>({
    mutationFn: ({ id }) =>
      api.post<InviteResponse>(`/users/${id}/invite/resend`),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation<AdminUser, unknown, { id: string }>({
    mutationFn: ({ id }) => api.post<AdminUser>(`/users/${id}/invite/revoke`),
    onSuccess: () => qc.invalidateQueries({ queryKey: userAdminKeys.all }),
  });
}
