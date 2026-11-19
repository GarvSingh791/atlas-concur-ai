"use client";

/**
 * Account recovery hooks — phase 12 follow-ups.
 *
 * Wired to backend/app/modules/identity/router.py:
 *   useForgotPassword    → POST /auth/forgot-password   (public; always 202)
 *   useResetPassword     → POST /auth/reset-password    (public; token in the body)
 *   useInvitePeek        → GET  /auth/invite/{token}    (public; generic 404)
 *   useAcceptInvite      → POST /auth/accept-invite     (public; token in the body)
 *   useVerifyEmail       → POST /auth/verify-email      (public; token in the body)
 *   useSendVerification  → POST /auth/send-verification (signed in)
 *
 * Public calls use `skipAuth`: no bearer is sent and a 4xx surfaces as a typed
 * ApiError for the form (never the refresh / redirect-to-login path). Tokens go
 * only in JSON bodies; nothing here stores or logs them.
 */

import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type {
  AcceptInviteRequest,
  AcceptedResponse,
  ForgotPasswordRequest,
  InvitePeekResponse,
  ResetPasswordRequest,
} from "@/lib/types/identity";

export function useForgotPassword() {
  return useMutation<AcceptedResponse, unknown, ForgotPasswordRequest>({
    mutationFn: (body) =>
      api.post<AcceptedResponse>("/auth/forgot-password", {
        body,
        skipAuth: true,
      }),
  });
}

export function useResetPassword() {
  return useMutation<null, unknown, ResetPasswordRequest>({
    mutationFn: (body) =>
      api.post<null>("/auth/reset-password", { body, skipAuth: true }),
  });
}

export function useInvitePeek(token: string) {
  return useQuery<InvitePeekResponse>({
    enabled: token.length > 0,
    queryKey: ["invite-peek", token],
    queryFn: () =>
      api.get<InvitePeekResponse>(`/auth/invite/${encodeURIComponent(token)}`, {
        skipAuth: true,
      }),
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
  });
}

export function useAcceptInvite() {
  return useMutation<null, unknown, AcceptInviteRequest>({
    mutationFn: (body) =>
      api.post<null>("/auth/accept-invite", { body, skipAuth: true }),
  });
}

export function useVerifyEmail() {
  return useMutation<null, unknown, { token: string }>({
    mutationFn: (body) =>
      api.post<null>("/auth/verify-email", { body, skipAuth: true }),
  });
}

export function useSendVerification() {
  return useMutation<AcceptedResponse, unknown, void>({
    mutationFn: () => api.post<AcceptedResponse>("/auth/send-verification"),
  });
}
