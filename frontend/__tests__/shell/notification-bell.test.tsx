import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { NotificationItem } from "@/lib/types/shell";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));

const hooks = vi.hoisted(() => ({
  useUnreadCount: vi.fn(),
  useNotifications: vi.fn(),
  useMarkNotificationRead: vi.fn(),
  useMarkAllNotificationsRead: vi.fn(),
}));
vi.mock("@/lib/query/notification-hooks", () => hooks);

import { useSession } from "@/lib/auth/session-provider";
import { NotificationBell } from "@/components/atlas/notification-bell";

const markOne = vi.fn();
const markAll = vi.fn();

function session(role: RoleKey) {
  vi.mocked(useSession).mockReturnValue({
    user: { id: "me", role },
    role,
    permissions: ROLE_PERMISSIONS[role],
  } as never);
}

function item(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: "decision:1",
    kind: "approved",
    title: "Expense report approved",
    body: "Q3 offsite",
    subject_type: "expense_report",
    subject_id: "r1",
    link: "/expenses?report=r1",
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    read: false,
    ...over,
  };
}

function setFeed(unread: number, items: NotificationItem[]) {
  hooks.useUnreadCount.mockReturnValue({ data: { unread } });
  hooks.useNotifications.mockReturnValue({
    data: { items, total: items.length, page: 1, size: 20, pages: 1 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
}

beforeEach(() => {
  push.mockReset();
  markOne.mockReset();
  markAll.mockReset();
  hooks.useMarkNotificationRead.mockReturnValue({ mutate: markOne });
  hooks.useMarkAllNotificationsRead.mockReturnValue({
    mutate: markAll,
    isPending: false,
  });
  session("employee");
});
afterEach(cleanup);

const bell = () => screen.getByTestId("notification-bell");

describe("NotificationBell — badge", () => {
  it("shows no badge (and no dot) when nothing is unread", () => {
    setFeed(0, []);
    render(<NotificationBell />);
    expect(screen.queryByTestId("notification-badge")).toBeNull();
    expect(bell().getAttribute("aria-label")).toBe(
      "Notifications, none unread",
    );
    expect(bell().querySelector(".bg-destructive")).toBeNull();
  });

  it("shows the server count when > 0, announced politely", () => {
    setFeed(3, [item()]);
    render(<NotificationBell />);
    expect(screen.getByTestId("notification-badge").textContent).toBe("3");
    expect(bell().getAttribute("aria-label")).toBe("Notifications, 3 unread");
    expect(screen.getByRole("status").textContent).toBe(
      "3 unread notifications",
    );
  });

  it("caps the badge text at 99+", () => {
    setFeed(250, []);
    render(<NotificationBell />);
    expect(screen.getByTestId("notification-badge").textContent).toBe("99+");
  });

  it("only fetches the list while the panel is open", () => {
    setFeed(1, [item()]);
    render(<NotificationBell />);
    expect(hooks.useNotifications).toHaveBeenLastCalledWith(false);
    fireEvent.click(bell());
    expect(hooks.useNotifications).toHaveBeenLastCalledWith(true);
  });
});

describe("NotificationBell — panel", () => {
  it("lists items with relative time and an empty state", () => {
    setFeed(1, [item()]);
    render(<NotificationBell />);
    fireEvent.click(bell());
    expect(screen.getByText("Expense report approved")).toBeTruthy();
    expect(screen.getByText("5m ago")).toBeTruthy();
    cleanup();

    setFeed(0, []);
    render(<NotificationBell />);
    fireEvent.click(bell());
    expect(screen.getByTestId("notification-empty")).toBeTruthy();
    expect(
      (screen.getByTestId("notification-mark-all") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("clicking an unread item marks it read and navigates to its subject", () => {
    setFeed(1, [item()]);
    render(<NotificationBell />);
    fireEvent.click(bell());
    fireEvent.click(screen.getByTestId("notification-item-decision:1"));
    expect(markOne).toHaveBeenCalledWith("decision:1");
    expect(push).toHaveBeenCalledWith("/expenses?report=r1");
    expect(screen.queryByTestId("notification-panel")).toBeNull();
  });

  it("a read item navigates without re-marking", () => {
    setFeed(0, [item({ read: true })]);
    render(<NotificationBell />);
    fireEvent.click(bell());
    fireEvent.click(screen.getByTestId("notification-item-decision:1"));
    expect(markOne).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/expenses?report=r1");
  });

  it("never navigates to a page the role cannot open", () => {
    session("finance_admin");
    setFeed(1, [item()]); // /expenses is not a finance page
    render(<NotificationBell />);
    fireEvent.click(bell());
    fireEvent.click(screen.getByTestId("notification-item-decision:1"));
    expect(markOne).toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("Mark all read calls the server", () => {
    setFeed(2, [item(), item({ id: "decision:2" })]);
    render(<NotificationBell />);
    fireEvent.click(bell());
    fireEvent.click(screen.getByTestId("notification-mark-all"));
    expect(markAll).toHaveBeenCalledTimes(1);
  });

  it("is keyboard operable: arrows move focus, Escape closes and refocuses the bell", () => {
    setFeed(2, [item(), item({ id: "decision:2", title: "Second" })]);
    render(<NotificationBell />);
    fireEvent.click(bell());
    const panel = screen.getByTestId("notification-panel");
    fireEvent.keyDown(panel, { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      screen.getByTestId("notification-item-decision:1"),
    );
    fireEvent.keyDown(panel, { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      screen.getByTestId("notification-item-decision:2"),
    );
    fireEvent.keyDown(panel, { key: "ArrowUp" });
    expect(document.activeElement).toBe(
      screen.getByTestId("notification-item-decision:1"),
    );
    fireEvent.keyDown(panel, { key: "Escape" });
    expect(screen.queryByTestId("notification-panel")).toBeNull();
    expect(document.activeElement).toBe(bell());
  });
});
