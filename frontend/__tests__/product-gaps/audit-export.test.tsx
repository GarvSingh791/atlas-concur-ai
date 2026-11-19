/** Product gap 8 — the audit page exports with the SAME filters + order as its table. */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

const { useAuditLog, downloadFile } = vi.hoisted(() => ({
  useAuditLog: vi.fn(),
  downloadFile: vi.fn(),
}));
vi.mock("@/lib/query/audit-hooks", () => ({ useAuditLog }));
vi.mock("@/lib/api/download", async (orig) => ({
  ...(await orig<typeof import("@/lib/api/download")>()),
  downloadFile,
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import AuditPage from "@/app/(app)/audit/page";

afterEach(cleanup);

describe("Audit export", () => {
  it("passes the applied filters and sort to GET /audit/export.csv", async () => {
    useAuditLog.mockReturnValue({
      data: { items: [], total: 0, page: 1, size: 20, pages: 0 },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    });
    downloadFile.mockResolvedValue(undefined);
    render(<AuditPage />);
    fireEvent.change(screen.getByLabelText("Action"), {
      target: { value: "trip.created" },
    });
    fireEvent.click(screen.getByRole("button", { name: /apply filters/i }));
    fireEvent.click(screen.getByTestId("export-csv"));
    await waitFor(() => expect(downloadFile).toHaveBeenCalled());
    const [path, params, filename] = downloadFile.mock.calls[0];
    expect(path).toBe("/audit/export.csv");
    expect(params).toEqual({
      action: "trip.created",
      entity_type: "",
      sort: "created_at:desc",
    });
    expect(filename).toMatch(/^atlas-audit-log-\d{4}-\d{2}-\d{2}\.csv$/);
  });
});
