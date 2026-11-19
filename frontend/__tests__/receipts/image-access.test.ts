import { describe, it, expect } from "vitest";
import {
  MAX_LINK_TTL_MS,
  MIN_REFRESH_MS,
  REFRESH_LEAD_MS,
  msUntilRefresh,
} from "@/lib/receipts/image-access";

const NOW = Date.parse("2026-11-08T12:00:00Z");

describe("msUntilRefresh", () => {
  it("returns false when there is no link / no expiry", () => {
    expect(msUntilRefresh(null, NOW, NOW)).toBe(false);
    expect(msUntilRefresh(undefined, NOW, NOW)).toBe(false);
    expect(msUntilRefresh({}, NOW, NOW)).toBe(false);
    expect(
      msUntilRefresh(
        { image_access_expires_at: null, image_access_expires_in: null },
        NOW,
        NOW,
      ),
    ).toBe(false);
  });

  it("returns false for an unparseable absolute expiry with no lifetime", () => {
    expect(
      msUntilRefresh({ image_access_expires_at: "not-a-date" }, NOW, NOW),
    ).toBe(false);
  });

  it("refreshes REFRESH_LEAD_MS before the lifetime ends, measured from arrival", () => {
    expect(msUntilRefresh({ image_access_expires_in: 300 }, NOW, NOW)).toBe(
      300_000 - REFRESH_LEAD_MS,
    );
    // Evaluated 100 s after the response arrived.
    expect(
      msUntilRefresh({ image_access_expires_in: 300 }, NOW, NOW + 100_000),
    ).toBe(200_000 - REFRESH_LEAD_MS);
  });

  it("ignores a skewed absolute timestamp when the relative lifetime is known", () => {
    // Browser clock ~290 s AHEAD of the server: the absolute expiry looks
    // almost passed, but the link really has its full 300 s.
    const skewedExpiry = new Date(NOW + 10_000).toISOString();
    expect(
      msUntilRefresh(
        { image_access_expires_at: skewedExpiry, image_access_expires_in: 300 },
        NOW,
        NOW,
      ),
    ).toBe(300_000 - REFRESH_LEAD_MS);
  });

  it("clamps an absolute-only expiry to the 300 s cap from arrival (slow clock)", () => {
    // Browser clock far BEHIND the server: the absolute expiry looks hours
    // away; the link can never outlive 300 s from when it arrived.
    const farFuture = new Date(NOW + 3_600_000).toISOString();
    expect(
      msUntilRefresh({ image_access_expires_at: farFuture }, NOW, NOW),
    ).toBe(MAX_LINK_TTL_MS - REFRESH_LEAD_MS);
  });

  it("falls back to the absolute expiry when no lifetime is sent", () => {
    const expiresAt = new Date(NOW + 120_000).toISOString();
    expect(
      msUntilRefresh({ image_access_expires_at: expiresAt }, NOW, NOW),
    ).toBe(120_000 - REFRESH_LEAD_MS);
  });

  it("never schedules sooner than MIN_REFRESH_MS near or past expiry", () => {
    expect(msUntilRefresh({ image_access_expires_in: 10 }, NOW, NOW)).toBe(
      MIN_REFRESH_MS,
    );
    expect(
      msUntilRefresh({ image_access_expires_in: 300 }, NOW, NOW + 400_000),
    ).toBe(MIN_REFRESH_MS);
  });
});
