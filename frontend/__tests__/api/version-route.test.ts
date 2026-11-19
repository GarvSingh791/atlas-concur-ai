// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "@/app/api/version/route";

describe("GET /api/version", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("reports the Vercel build commit", async () => {
    vi.stubEnv(
      "VERCEL_GIT_COMMIT_SHA",
      "0123456789abcdef0123456789abcdef01234567",
    );
    const body = await GET().json();
    expect(body).toEqual({
      commit: "0123456789abcdef0123456789abcdef01234567",
    });
  });

  it('falls back to "local" outside Vercel', async () => {
    vi.stubEnv("VERCEL_GIT_COMMIT_SHA", "");
    const body = await GET().json();
    expect(body).toEqual({ commit: "local" });
  });
});
