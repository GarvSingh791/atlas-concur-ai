/** Product gap 8 — authenticated CSV download (bearer → blob) + error toast. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

const { recover, showToast } = vi.hoisted(() => ({
  recover: vi.fn(),
  showToast: vi.fn(),
}));
vi.mock("@/lib/api/client", () => ({ recoverFromUnauthorized: recover }));
vi.mock("@/lib/auth/token-store", () => ({ getAccessToken: () => "tok-1" }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));

import { buildDownloadUrl, downloadFile, todayStamp } from "@/lib/api/download";
import { ExportCsvButton } from "@/components/exports/export-csv-button";
import { ApiError } from "@/lib/types/api";

const fetchMock = vi.fn();
const click = vi.fn();

function csvResponse(status = 200): Response {
  return new Response(
    status === 200
      ? "﻿A\r\n"
      : JSON.stringify({
          error: { code: "FORBIDDEN", message: "Nope.", correlation_id: "c-1" },
        }),
    {
      status,
      headers: {
        "Content-Type": status === 200 ? "text/csv" : "application/json",
      },
    },
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  recover.mockReset();
  showToast.mockReset();
  click.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(click);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("downloadFile", () => {
  it("builds the URL without blank params", () => {
    expect(
      buildDownloadUrl("/approvals/export.csv", {
        department: "R&D",
        exception_type: "",
        page: undefined,
      }),
    ).toMatch(/\/approvals\/export\.csv\?department=R%26D$/);
    expect(todayStamp(new Date(2026, 0, 5))).toBe("2026-01-05");
  });

  it("sends the bearer token and hands the blob to the browser", async () => {
    fetchMock.mockResolvedValueOnce(csvResponse());
    await downloadFile(
      "/audit/export.csv",
      { action: "trip.created" },
      "f.csv",
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/audit\/export\.csv\?action=trip\.created$/);
    expect(init.headers.Authorization).toBe("Bearer tok-1");
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(csvResponse());
    recover.mockResolvedValueOnce("tok-2");
    await downloadFile("/approvals/export.csv", {}, "f.csv");
    expect(recover).toHaveBeenCalledWith("tok-1");
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe(
      "Bearer tok-2",
    );
  });

  it("throws a typed ApiError on a non-2xx and never clicks", async () => {
    fetchMock.mockResolvedValueOnce(csvResponse(403));
    await expect(
      downloadFile("/approvals/export.csv", {}, "f.csv"),
    ).rejects.toMatchObject({
      status: 403,
      message: "Nope.",
    });
    expect(click).not.toHaveBeenCalled();
  });

  it("gives up when the session cannot be refreshed", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 401 }));
    recover.mockResolvedValueOnce(null);
    await expect(downloadFile("/x.csv", {}, "f.csv")).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("ExportCsvButton", () => {
  it("downloads a dated file", async () => {
    fetchMock.mockResolvedValueOnce(csvResponse());
    render(
      <ExportCsvButton path="/approvals/export.csv" filenameStem="approvals" />,
    );
    fireEvent.click(screen.getByTestId("export-csv"));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(showToast).not.toHaveBeenCalled();
  });

  it("shows an error toast when the export fails", async () => {
    fetchMock.mockResolvedValueOnce(csvResponse(403));
    render(
      <ExportCsvButton path="/approvals/export.csv" filenameStem="approvals" />,
    );
    fireEvent.click(screen.getByTestId("export-csv"));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith({
        variant: "error",
        title: "Export failed",
        description: "Nope.",
      }),
    );
    expect(
      (screen.getByTestId("export-csv") as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});
