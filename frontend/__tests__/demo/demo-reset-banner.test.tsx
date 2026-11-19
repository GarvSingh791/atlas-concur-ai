/**
 * Phase 12 — the scheduled demo reset in the "Demo environment" banner:
 * "Demo data resets daily — next reset ~HH:MM" (visitor's local time) when the
 * server reports an enabled schedule (GET /public/config demo_reset).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, render, screen } from "@testing-library/react";

import {
  DemoBanner,
  formatResetTime,
  resetScheduleText,
} from "@/components/atlas/demo-banner";

const cfg = vi.hoisted(() => ({ usePublicConfig: vi.fn() }));
vi.mock("@/lib/query/public-config-hooks", () => cfg);

const NEXT = "2026-11-16T04:00:00Z";
const localHHMM = new Date(NEXT).toLocaleTimeString([], {
  hour: "2-digit",
  minute: "2-digit",
});

function schedule(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    interval_hours: 24,
    next_reset_at: NEXT,
    last_reset_at: "2026-11-15T04:01:00Z",
    ...over,
  };
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

describe("Demo reset schedule in the banner", () => {
  it("shows the daily reset and the next reset in local time", () => {
    cfg.usePublicConfig.mockReturnValue({
      data: { demo_mode: true, demo_reset: schedule() },
    });
    render(<DemoBanner />);
    expect(screen.getByTestId("demo-reset-schedule").textContent).toBe(
      ` Demo data resets daily — next reset ~${localHHMM}.`,
    );
    // The phase-11 message is still there.
    expect(screen.getByTestId("demo-banner").textContent).toContain(
      "periodically reset",
    );
  });

  it("is absent when the schedule is off", () => {
    cfg.usePublicConfig.mockReturnValue({
      data: {
        demo_mode: true,
        demo_reset: {
          enabled: false,
          interval_hours: null,
          next_reset_at: null,
          last_reset_at: null,
        },
      },
    });
    render(<DemoBanner />);
    expect(screen.getByTestId("demo-banner")).toBeTruthy();
    expect(screen.queryByTestId("demo-reset-schedule")).toBeNull();
  });

  it("is absent on an older server without demo_reset", () => {
    cfg.usePublicConfig.mockReturnValue({ data: { demo_mode: true } });
    render(<DemoBanner />);
    expect(screen.queryByTestId("demo-reset-schedule")).toBeNull();
  });

  it("never shows without demo mode, even with a schedule", () => {
    cfg.usePublicConfig.mockReturnValue({
      data: { demo_mode: false, demo_reset: schedule() },
    });
    render(<DemoBanner />);
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });
});

describe("resetScheduleText", () => {
  it("names other intervals", () => {
    expect(resetScheduleText(schedule({ interval_hours: 6 }))).toBe(
      `Demo data resets every 6 hours — next reset ~${localHHMM}.`,
    );
    expect(resetScheduleText(schedule({ interval_hours: 1 }))).toBe(
      `Demo data resets every hour — next reset ~${localHHMM}.`,
    );
  });

  it("omits the time when the next reset is not known yet", () => {
    expect(resetScheduleText(schedule({ next_reset_at: null }))).toBe(
      "Demo data resets daily.",
    );
    expect(resetScheduleText(schedule({ next_reset_at: "garbage" }))).toBe(
      "Demo data resets daily.",
    );
  });

  it("is null when disabled or absent", () => {
    expect(resetScheduleText(undefined)).toBeNull();
    expect(resetScheduleText(schedule({ enabled: false }))).toBeNull();
  });

  it("formats in local time", () => {
    expect(formatResetTime(NEXT)).toBe(localHHMM);
    expect(formatResetTime(null)).toBeNull();
  });
});
