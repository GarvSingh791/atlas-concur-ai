"use client";

/**
 * Notification hooks — product gap 3 (app-shell bell).
 *
 * Wired to backend/app/modules/notifications/router.py:
 *   useUnreadCount        → GET  /notifications/unread-count   (polled)
 *   useNotifications      → GET  /notifications                (while the popover is open)
 *   useMarkNotificationRead → POST /notifications/{id}/read
 *   useMarkAllNotificationsRead → POST /notifications/read-all
 *
 * The bell refreshes on window focus and every 60 s (overriding the app-wide
 * refetchOnWindowFocus:false default — a stale badge is the bug being fixed).
 * Mutations are optimistic-free: they invalidate and let the server answer.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { PaginatedResponse } from "@/lib/types/api";
import type {
  MarkReadResponse,
  NotificationItem,
  UnreadCountResponse,
} from "@/lib/types/shell";

export const NOTIFICATION_POLL_MS = 60_000;
const LIST_SIZE = 20;

export const notificationKeys = {
  all: ["notifications"] as const,
  unread: () => ["notifications", "unread"] as const,
  list: () => ["notifications", "list"] as const,
};

export function useUnreadCount(enabled = true) {
  return useQuery({
    queryKey: notificationKeys.unread(),
    queryFn: () => api.get<UnreadCountResponse>("/notifications/unread-count"),
    enabled,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    refetchInterval: NOTIFICATION_POLL_MS,
  });
}

export function useNotifications(enabled: boolean) {
  return useQuery({
    queryKey: notificationKeys.list(),
    queryFn: () =>
      api.get<PaginatedResponse<NotificationItem>>("/notifications", {
        params: { size: LIST_SIZE },
      }),
    enabled,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    refetchInterval: enabled ? NOTIFICATION_POLL_MS : false,
  });
}

export function useMarkNotificationRead() {
  const qc = useQueryClient();
  return useMutation<MarkReadResponse, unknown, string>({
    mutationFn: (id) =>
      api.post<MarkReadResponse>(
        `/notifications/${encodeURIComponent(id)}/read`,
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  });
}

export function useMarkAllNotificationsRead() {
  const qc = useQueryClient();
  return useMutation<MarkReadResponse, unknown, void>({
    mutationFn: () => api.post<MarkReadResponse>("/notifications/read-all"),
    onSettled: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  });
}
