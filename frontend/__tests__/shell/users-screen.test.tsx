import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";

import UsersPage from "@/app/(app)/users/page";
import { ApiError } from "@/lib/types/api";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";
import type { AdminUser, GradeOption } from "@/lib/types/shell";

vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
const hooks = vi.hoisted(() => ({
  useAdminUsers: vi.fn(),
  useManagerOptions: vi.fn(),
  useCreateUser: vi.fn(),
  useUpdateUser: vi.fn(),
  useSetUserActive: vi.fn(),
  useGrades: vi.fn(),
  // Known limits (organisation-aware login): the org's login code.
  useOrganizationLoginCode: vi.fn(),
}));
vi.mock("@/lib/query/user-admin-hooks", () => hooks);

import { useSession } from "@/lib/auth/session-provider";

const create = vi.fn();
const update = vi.fn();
const setActive = vi.fn();

function session(role: RoleKey, id = "fin") {
  vi.mocked(useSession).mockReturnValue({
    user: { id, role },
    role,
    permissions: ROLE_PERMISSIONS[role],
  } as never);
}

function user(over: Partial<AdminUser>): AdminUser {
  return {
    id: "u1",
    name: "Erin Employee",
    email: "erin@atlas.test",
    role: "employee",
    department: "Sales",
    manager_id: "m1",
    manager_name: "Max Manager",
    status: "active",
    organization_id: "o",
    created_at: "2026-01-01T00:00:00Z",
    grade: null,
    ...over,
  };
}

// Mirrors GET /grades (config/constants/grades.py), most senior first.
const GRADES: GradeOption[] = [
  {
    code: "CSUITE",
    title: "Chief Officer",
    short: "C-suite",
    rank: 80,
    entitlements: ["AIR-04"],
  },
  {
    code: "SVP",
    title: "Senior Vice President",
    short: "SVP",
    rank: 70,
    entitlements: ["AIR-04"],
  },
  {
    code: "VP",
    title: "Vice President",
    short: "VP",
    rank: 60,
    entitlements: [],
  },
  {
    code: "IC",
    title: "Individual Contributor",
    short: "IC",
    rank: 20,
    entitlements: [],
  },
];

const USERS = [
  user({ grade: "IC" }),
  user({
    id: "m1",
    name: "Max Manager",
    email: "max@atlas.test",
    role: "manager",
    manager_id: null,
    manager_name: null,
    department: null,
  }),
  user({
    id: "fin",
    name: "Fay Finance",
    email: "fay@atlas.test",
    role: "finance_admin",
    manager_id: null,
    manager_name: null,
  }),
  user({ id: "gone", name: "Gus Gone", status: "inactive" }),
];

beforeEach(() => {
  for (const m of [create, update, setActive]) m.mockReset();
  hooks.useAdminUsers.mockReturnValue({
    data: { items: USERS, total: USERS.length, page: 1, size: 20, pages: 1 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  hooks.useManagerOptions.mockReturnValue({
    data: USERS.filter((u) => u.role !== "employee"),
    isLoading: false,
  });
  hooks.useCreateUser.mockReturnValue({ mutate: create, isPending: false });
  hooks.useUpdateUser.mockReturnValue({ mutate: update, isPending: false });
  hooks.useSetUserActive.mockReturnValue({
    mutate: setActive,
    isPending: false,
  });
  hooks.useGrades.mockReturnValue({ data: GRADES, isLoading: false });
  hooks.useOrganizationLoginCode.mockReturnValue({
    data: { id: "o", name: "Atlas Demo Corp", login_code: "atlas-demo-corp" },
  });
  session("finance_admin");
});
afterEach(cleanup);

describe("Users admin page", () => {
  it("renders the table with role, manager, department and status", () => {
    render(<UsersPage />);
    const table = screen.getByRole("table", { name: "Users" });
    const erin = within(screen.getByTestId("user-row-u1"));
    expect(erin.getByText("Erin Employee")).toBeTruthy();
    expect(erin.getByText("erin@atlas.test")).toBeTruthy();
    expect(erin.getByText("Employee")).toBeTruthy();
    expect(erin.getByText("Max Manager")).toBeTruthy();
    expect(erin.getByText("Sales")).toBeTruthy();
    expect(erin.getByText("Active")).toBeTruthy();
    expect(
      within(screen.getByTestId("user-row-gone")).getByText("Inactive"),
    ).toBeTruthy();
    expect(within(table).getAllByRole("row")).toHaveLength(USERS.length + 1);
  });

  it("is a finance-only page: other roles get a notice and no fetch", () => {
    session("manager", "m1");
    render(<UsersPage />);
    expect(screen.getByText("Finance administrators only")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(hooks.useAdminUsers).toHaveBeenLastCalledWith(
      expect.anything(),
      false,
    );
  });

  it("shows the organisation login code read-only to finance admins", () => {
    render(<UsersPage />);
    const line = screen.getByTestId("org-login-code");
    expect(line.textContent).toContain("atlas-demo-corp");
    expect(within(line).queryByRole("textbox")).toBeNull();
    expect(hooks.useOrganizationLoginCode).toHaveBeenLastCalledWith(true);
  });

  it("never fetches or shows the login code for other roles", () => {
    session("manager", "m1");
    render(<UsersPage />);
    expect(screen.queryByTestId("org-login-code")).toBeNull();
    expect(hooks.useOrganizationLoginCode).toHaveBeenLastCalledWith(false);
  });

  it("sends search and filters to the server", () => {
    render(<UsersPage />);
    fireEvent.change(screen.getByLabelText("Filter by role"), {
      target: { value: "manager" },
    });
    fireEvent.change(screen.getByLabelText("Filter by status"), {
      target: { value: "inactive" },
    });
    const params = hooks.useAdminUsers.mock.lastCall?.[0];
    expect(params).toMatchObject({ role: "manager", status: "inactive" });
  });

  it("the admin cannot deactivate their own row", () => {
    render(<UsersPage />);
    const own = within(screen.getByTestId("user-row-fin")).getByRole("button", {
      name: "Deactivate Fay Finance",
    }) as HTMLButtonElement;
    expect(own.disabled).toBe(true);
    const other = within(screen.getByTestId("user-row-u1")).getByRole(
      "button",
      { name: "Deactivate Erin Employee" },
    ) as HTMLButtonElement;
    expect(other.disabled).toBe(false);
  });

  it("Add user posts the form and shows the server's error inline", () => {
    create.mockImplementation((_body, opts) =>
      opts.onError(
        new ApiError(
          {
            code: "EMAIL_TAKEN",
            message: "A user with this email already exists.",
            correlation_id: "c",
          },
          409,
        ),
      ),
    );
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: /Add user/ }));
    fireEvent.change(screen.getByLabelText("Full name"), {
      target: { value: " New Hire " },
    });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "erin@atlas.test" },
    });
    fireEvent.change(screen.getByLabelText("Role"), {
      target: { value: "manager" },
    });
    fireEvent.change(screen.getByLabelText("Manager"), {
      target: { value: "m1" },
    });
    fireEvent.change(screen.getByLabelText("Initial password"), {
      target: { value: "Correct-Horse-42" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create user" }));

    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0][0]).toEqual({
      name: "New Hire",
      email: "erin@atlas.test",
      role: "manager",
      manager_id: "m1",
      department: null,
      password: "Correct-Horse-42",
    });
    expect(screen.getByTestId("user-form-error").textContent).toBe(
      "A user with this email already exists.",
    );
  });

  it("Add user refuses an incomplete form without calling the server", () => {
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: /Add user/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create user" }));
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByTestId("user-form-error")).toBeTruthy();
  });

  it("Edit sends only the changed fields and never offers self as manager", () => {
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Max Manager" }));
    const managerSelect = screen.getByLabelText("Manager") as HTMLSelectElement;
    const optionValues = Array.from(managerSelect.options).map((o) => o.value);
    expect(optionValues).not.toContain("m1");
    fireEvent.change(screen.getByLabelText("Department"), {
      target: { value: "Ops" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(update).toHaveBeenCalledWith(
      { id: "m1", changes: { department: "Ops" } },
      expect.anything(),
    );
  });

  it("Deactivate asks for confirmation, then calls the server", () => {
    render(<UsersPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "Deactivate Erin Employee" }),
    );
    expect(setActive).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Deactivate" }));
    expect(setActive).toHaveBeenCalledWith(
      { id: "u1", active: false },
      expect.anything(),
    );
  });

  it("Reactivate is offered for inactive users", () => {
    render(<UsersPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "Reactivate Gus Gone" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Reactivate" }));
    expect(setActive).toHaveBeenCalledWith(
      { id: "gone", active: true },
      expect.anything(),
    );
  });
});

describe("Users admin page — traveller grades (AIR-04)", () => {
  it("shows each user's grade title, or Not recorded", () => {
    render(<UsersPage />);
    expect(screen.getByTestId("user-grade-u1").textContent).toBe(
      "Individual Contributor (IC)",
    );
    expect(screen.getByTestId("user-grade-m1").textContent).toBe(
      "Not recorded",
    );
    expect(hooks.useGrades).toHaveBeenLastCalledWith(true);
  });

  it("does not fetch the ladder for non-admins", () => {
    session("manager", "m1");
    render(<UsersPage />);
    expect(hooks.useGrades).toHaveBeenLastCalledWith(false);
  });

  it("Add user sends the chosen grade code", () => {
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: /Add user/ }));
    const select = screen.getByLabelText("Grade") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "",
      "CSUITE",
      "SVP",
      "VP",
      "IC",
    ]);
    fireEvent.change(screen.getByLabelText("Full name"), {
      target: { value: "Sam Senior" },
    });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "sam@atlas.test" },
    });
    fireEvent.change(select, { target: { value: "SVP" } });
    fireEvent.change(screen.getByLabelText("Initial password"), {
      target: { value: "Correct-Horse-42" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create user" }));
    expect(create.mock.calls[0][0]).toMatchObject({ grade: "SVP" });
  });

  it("Edit sends a grade change, and null to clear it", () => {
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Max Manager" }));
    fireEvent.change(screen.getByLabelText("Grade"), {
      target: { value: "VP" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(update).toHaveBeenLastCalledWith(
      { id: "m1", changes: { grade: "VP" } },
      expect.anything(),
    );
    cleanup();
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Erin Employee" }));
    fireEvent.change(screen.getByLabelText("Grade"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(update).toHaveBeenLastCalledWith(
      { id: "u1", changes: { grade: null } },
      expect.anything(),
    );
  });

  it("the admin's own grade is read-only and never sent", () => {
    render(<UsersPage />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Fay Finance" }));
    const select = screen.getByLabelText("Grade") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(screen.getByText(/can’t change your own grade/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Department"), {
      target: { value: "Treasury" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(update).toHaveBeenLastCalledWith(
      { id: "fin", changes: { department: "Treasury" } },
      expect.anything(),
    );
  });
});
