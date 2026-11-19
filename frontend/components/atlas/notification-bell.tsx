"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  Banknote,
  Bell,
  CheckCheck,
  CheckCircle,
  ClipboardList,
  Flag,
  Loader2,
  RotateCcw,
  XCircle,
} from "lucide-react";

import { useSession } from "@/lib/auth/session-provider";
import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
  useUnreadCount,
} from "@/lib/query/notification-hooks";
import { resolveLink } from "@/lib/shell/links";
import { relativeTime } from "@/lib/shell/relative-time";
import type { NotificationItem, NotificationKind } from "@/lib/types/shell";
import { cn } from "@/lib/utils";

/**
 * Notification bell — product gap 3 (replaces the "Presentational Only" bell
 * that always showed a red dot).
 *
 * - Badge with the server's unread count, rendered ONLY when > 0 (absolutely
 *   positioned, so its appearance never shifts the layout).
 * - The count is announced through a polite live region.
 * - Click opens a panel: newest-first list with kind icons + relative time,
 *   "Mark all read", empty / loading / error states. ArrowUp/ArrowDown move
 *   between items, Escape closes and returns focus to the bell.
 * - Activating an item marks it read (server-side) and navigates to its subject
 *   (only if the session may open that page — resolveLink).
 * - Refetches on window focus and every 60 s (notification-hooks).
 */

const KIND_ICON: Record<NotificationKind, typeof Bell> = {
  approval_pending: ClipboardList,
  approval_escalated: AlertTriangle,
  approved: CheckCircle,
  rejected: XCircle,
  returned: RotateCcw,
  reimbursed: Banknote,
  completed: Flag,
};

const KIND_TONE: Record<NotificationKind, string> = {
  approval_pending: "text-[var(--color-primary-600)]",
  approval_escalated: "text-[var(--color-warning)]",
  approved: "text-[var(--color-success)]",
  rejected: "text-[var(--color-error)]",
  returned: "text-[var(--color-warning)]",
  reimbursed: "text-[var(--color-success)]",
  completed: "text-[var(--color-primary-600)]",
};

function badgeText(count: number): string {
  return count > 99 ? "99+" : String(count);
}

export function NotificationBell() {
  const router = useRouter();
  const { permissions, role } = useSession();
  const [open, setOpen] = React.useState(false);
  const unread = useUnreadCount();
  const list = useNotifications(open);
  const markOne = useMarkNotificationRead();
  const markAll = useMarkAllNotificationsRead();

  const rootRef = React.useRef<HTMLDivElement>(null);
  const buttonRef = React.useRef<HTMLButtonElement>(null);
  const listRef = React.useRef<HTMLUListElement>(null);
  const panelId = React.useId();

  const count = unread.data?.unread ?? 0;
  const items = list.data?.items ?? [];

  const close = React.useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) buttonRef.current?.focus();
  }, []);

  // Click outside closes the panel.
  React.useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        close(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open, close]);

  const focusItem = (delta: number) => {
    const buttons = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>(
        "button[data-notification-item]",
      ) ?? [],
    );
    if (buttons.length === 0) return;
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const next =
      current === -1
        ? delta > 0
          ? 0
          : buttons.length - 1
        : (current + delta + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };

  const onPanelKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      focusItem(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusItem(-1);
    }
  };

  const activate = (item: NotificationItem) => {
    if (!item.read) markOne.mutate(item.id);
    const href = resolveLink(item.link, null, permissions, role);
    close(false);
    if (href) router.push(href);
  };

  return (
    <div ref={rootRef} className="relative" onKeyDown={onPanelKeyDown}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="relative rounded-md p-2 text-muted-foreground hover:bg-secondary hover:text-foreground focus-visible:atlas-focus-ring transition-colors"
        aria-label={
          count > 0
            ? `Notifications, ${count} unread`
            : "Notifications, none unread"
        }
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-haspopup="true"
        data-testid="notification-bell"
      >
        <Bell className="h-5 w-5" aria-hidden="true" />
        {count > 0 && (
          <span
            className="absolute right-0.5 top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-semibold leading-none text-white ring-2 ring-card"
            data-testid="notification-badge"
            aria-hidden="true"
          >
            {badgeText(count)}
          </span>
        )}
      </button>
      <span className="sr-only" aria-live="polite" role="status">
        {count > 0
          ? `${count} unread notification${count === 1 ? "" : "s"}`
          : ""}
      </span>

      {open && (
        <div
          id={panelId}
          role="region"
          aria-label="Notifications"
          className="absolute right-0 top-full z-50 mt-2 w-[22rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-atlas-lg border border-border bg-card shadow-atlas-3"
          data-testid="notification-panel"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
            <h2 className="text-sm font-semibold text-foreground">
              Notifications
            </h2>
            <button
              type="button"
              onClick={() => markAll.mutate()}
              disabled={count === 0 || markAll.isPending}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-[var(--color-primary-600)] hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-50 focus-visible:atlas-focus-ring"
              data-testid="notification-mark-all"
            >
              <CheckCheck className="h-3.5 w-3.5" aria-hidden="true" />
              Mark all read
            </button>
          </div>
          <PanelBody
            isLoading={list.isLoading}
            isError={list.isError}
            onRetry={() => list.refetch()}
            items={items}
            listRef={listRef}
            onActivate={activate}
          />
        </div>
      )}
    </div>
  );
}

function PanelBody({
  isLoading,
  isError,
  onRetry,
  items,
  listRef,
  onActivate,
}: {
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  items: NotificationItem[];
  listRef: React.RefObject<HTMLUListElement>;
  onActivate: (item: NotificationItem) => void;
}) {
  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 px-4 py-8 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Loading notifications…
      </div>
    );
  }
  if (isError) {
    return (
      <div className="flex flex-col items-center gap-2 px-4 py-8 text-center text-sm text-muted-foreground">
        <p role="alert">Couldn’t load notifications.</p>
        <button
          type="button"
          onClick={onRetry}
          className="text-xs font-medium text-[var(--color-primary-600)] hover:underline"
        >
          Try again
        </button>
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div
        className="flex flex-col items-center gap-1 px-4 py-8 text-center"
        data-testid="notification-empty"
      >
        <Bell className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
        <p className="text-sm font-medium text-foreground">
          You’re all caught up
        </p>
        <p className="text-xs text-muted-foreground">
          Approvals and decisions on your reports and trips show up here.
        </p>
      </div>
    );
  }
  return (
    <ul
      ref={listRef}
      className="max-h-[24rem] overflow-y-auto py-1"
      aria-label="Notification list"
    >
      {items.map((item) => {
        const Icon = KIND_ICON[item.kind] ?? Bell;
        return (
          <li key={item.id}>
            <button
              type="button"
              data-notification-item
              data-testid={`notification-item-${item.id}`}
              onClick={() => onActivate(item)}
              className={cn(
                "flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-secondary focus-visible:bg-secondary focus-visible:outline-none",
                !item.read && "bg-[var(--color-primary-50)]",
              )}
            >
              <Icon
                className={cn("mt-0.5 h-4 w-4 shrink-0", KIND_TONE[item.kind])}
                aria-hidden="true"
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span
                  className={cn(
                    "truncate text-sm text-foreground",
                    !item.read && "font-semibold",
                  )}
                >
                  {item.title}
                  {!item.read && <span className="sr-only"> (unread)</span>}
                </span>
                <span className="line-clamp-2 text-xs text-muted-foreground">
                  {item.body}
                </span>
                <time
                  dateTime={item.created_at}
                  className="mt-0.5 text-[11px] text-muted-foreground"
                >
                  {relativeTime(item.created_at)}
                </time>
              </span>
              {!item.read && (
                <span
                  className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-[var(--color-primary-600)]"
                  aria-hidden="true"
                />
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
