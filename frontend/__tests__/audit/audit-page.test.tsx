import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
} from "@testing-library/react";

import AuditPage from "@/app/(app)/audit/page";
import { ApiError } from "@/lib/types/api";
import type { AuditLogItem } from "@/lib/types/audit";

// Mock the data hook so the page renders deterministic rows.
const { useAuditLog } = vi.hoisted(() => ({ useAuditLog: vi.fn() }));
vi.mock("@/lib/query/audit-hooks", () => ({ useAuditLog }));

afterEach(cleanup);

function row(overrides: Partial<AuditLogItem>): AuditLogItem {
  return {
    id: "a",
    action: "trip.created",
    entity_type: "trip",
    entity_id: "11111111-2222-3333-4444-555555555555",
    actor_id: "u-1",
    actor_name: "Erin Employee",
    before_state: null,
    after_state: { status: "draft" },
    created_at: "2026-06-23T12:00:00Z",
    ...overrides,
  };
}

const ITEMS: AuditLogItem[] = [
  row({ id: "a-1" }),
  row({
    id: "a-2",
    action: "approval.decided",
    entity_type: "approval",
    entity_id: "99999999-8888-7777-6666-555555555555",
    actor_id: null,
    actor_name: null,
    before_state: { current_status: "pending" },
    after_state: { current_status: "approved" },
  }),
];

function ok(items: AuditLogItem[] = ITEMS, total = items.length, page = 1) {
  return {
    data: { items, total, page, size: 20, pages: Math.ceil(total / 20) },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  };
}

function lastParams() {
  const calls = useAuditLog.mock.calls;
  return calls[calls.length - 1][0];
}

beforeEach(() => {
  useAuditLog.mockReset();
  useAuditLog.mockReturnValue(ok());
});

describe("AuditPage", () => {
  it("renders the heading and a row per audit event", () => {
    render(<AuditPage />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Audit Trail" }),
    ).toBeTruthy();
    const table = screen.getByRole("table");
    for (const header of [
      "Time",
      "Actor",
      "Action",
      "Entity type",
      "Entity ID",
    ]) {
      expect(within(table).getByText(header)).toBeTruthy();
    }
    expect(screen.getByText("trip.created")).toBeTruthy();
    expect(screen.getByText("approval.decided")).toBeTruthy();
    expect(screen.getByText("Erin Employee")).toBeTruthy();
  });

  it("labels system-initiated events (null actor) as System", () => {
    render(<AuditPage />);
    expect(screen.getByText("System")).toBeTruthy();
  });

  it("shows a truncated entity id with the full id available", () => {
    render(<AuditPage />);
    const cell = screen.getByTitle("11111111-2222-3333-4444-555555555555");
    expect(cell.textContent).toBe("11111111…");
  });

  it("requests page 1, newest first, with no filters by default", () => {
    render(<AuditPage />);
    expect(lastParams()).toEqual({
      page: 1,
      size: 20,
      sort: "created_at:desc",
      action: "",
      entity_type: "",
    });
  });

  it("applies the action / entity type filters on submit and resets to page 1", () => {
    useAuditLog.mockReturnValue(ok(ITEMS, 45));
    render(<AuditPage />);
    fireEvent.click(screen.getByRole("button", { name: "Page 2" }));
    expect(lastParams().page).toBe(2);

    fireEvent.change(screen.getByLabelText("Action"), {
      target: { value: " approval.decided " },
    });
    fireEvent.change(screen.getByLabelText("Entity type"), {
      target: { value: "approval" },
    });
    // Typing alone does not refetch with the draft.
    expect(lastParams().action).toBe("");

    fireEvent.click(screen.getByRole("button", { name: /apply filters/i }));
    expect(lastParams()).toMatchObject({
      page: 1,
      action: "approval.decided",
      entity_type: "approval",
    });

    fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect(lastParams()).toMatchObject({ action: "", entity_type: "" });
  });

  it("paginates via the table controls", () => {
    useAuditLog.mockReturnValue(ok(ITEMS, 45));
    render(<AuditPage />);
    expect(screen.getByText(/Showing 1–20 of 45/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(lastParams().page).toBe(2);
  });

  it("toggles the time sort between newest and oldest first", () => {
    render(<AuditPage />);
    const timeHeader = screen.getByText("Time");
    fireEvent.click(timeHeader);
    expect(lastParams().sort).toBe("created_at:asc");
    fireEvent.click(timeHeader);
    expect(lastParams().sort).toBe("created_at:desc");
  });

  it("expands a row to show the before / after snapshots", () => {
    render(<AuditPage />);
    const expanders = screen.getAllByRole("button", { name: "Expand row" });
    fireEvent.click(expanders[1]);
    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText(/"current_status": "pending"/)).toBeTruthy();
    expect(screen.getByText(/"current_status": "approved"/)).toBeTruthy();
  });

  it("renders the loading skeleton while the first page loads", () => {
    useAuditLog.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
      refetch: vi.fn(),
    });
    const { container } = render(<AuditPage />);
    expect(container.querySelectorAll(".atlas-shimmer").length).toBeGreaterThan(
      0,
    );
    expect(screen.queryByText("trip.created")).toBeNull();
  });

  it("renders the empty state when the org has no events", () => {
    useAuditLog.mockReturnValue(ok([], 0));
    render(<AuditPage />);
    expect(screen.getByText("No audit events yet")).toBeTruthy();
  });

  it("renders a filter-aware empty state", () => {
    render(<AuditPage />);
    useAuditLog.mockReturnValue(ok([], 0));
    fireEvent.change(screen.getByLabelText("Action"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: /apply filters/i }));
    expect(screen.getByText("No matching audit events")).toBeTruthy();
  });

  it("renders an error state with retry", () => {
    const refetch = vi.fn();
    useAuditLog.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("boom"),
      refetch,
    });
    render(<AuditPage />);
    expect(screen.getByText("Couldn’t load the audit trail")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
  });

  it("renders a finance-only message (no retry) on 403", () => {
    useAuditLog.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(
        { code: "FORBIDDEN", message: "Forbidden", correlation_id: "c" },
        403,
      ),
      refetch: vi.fn(),
    });
    render(<AuditPage />);
    expect(
      screen.getByText(/available to finance administrators/i),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
