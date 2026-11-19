/**
 * C7R issue 5 — Playwright HTML reports / traces embed live tokens and the
 * login password. The committed config may only produce them against local
 * targets, and only on explicit opt-in; anything else needs
 * E2E_ALLOW_SENSITIVE_ARTIFACTS=1. Artifact dirs are git-ignored.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

import {
  isLocalUrl,
  resolveArtifactPolicy,
} from "@/playwright.artifact-policy";

const LOCAL_APP = "http://localhost:3000";
const LOCAL_API = "http://localhost:8000/api/v1";
const REMOTE_API = "https://atlas-backend.example.com/api/v1";

describe("C7R-5 Playwright artifact policy", () => {
  it("defaults to list-only reporter and traces off", () => {
    const p = resolveArtifactPolicy({}, LOCAL_APP, LOCAL_API);
    expect(p.reporter).toEqual([["list"]]);
    expect(p.trace).toBe("off");
  });

  it("local target + opt-in enables html (never auto-opened) and traces", () => {
    const p = resolveArtifactPolicy(
      { E2E_HTML_REPORT: "1", E2E_TRACE: "retain-on-failure" },
      LOCAL_APP,
      "http://127.0.0.1:8000/api/v1",
    );
    expect(p.reporter).toEqual([
      ["list"],
      ["html", { open: "never", outputFolder: "playwright-report" }],
    ]);
    expect(p.trace).toBe("retain-on-failure");
  });

  it("a remote API blocks html + traces even when requested", () => {
    const p = resolveArtifactPolicy(
      { E2E_HTML_REPORT: "1", E2E_TRACE: "on" },
      LOCAL_APP,
      REMOTE_API,
    );
    expect(p.targetIsLocal).toBe(false);
    expect(p.reporter).toEqual([["list"]]);
    expect(p.trace).toBe("off");
  });

  it("remote targets need the explicit sensitive-artifacts opt-in", () => {
    const p = resolveArtifactPolicy(
      {
        E2E_HTML_REPORT: "1",
        E2E_TRACE: "on",
        E2E_ALLOW_SENSITIVE_ARTIFACTS: "1",
      },
      "https://atlas.example.com",
      REMOTE_API,
    );
    expect(p.reporter).toHaveLength(2);
    expect(p.trace).toBe("on");
  });

  it("unknown trace modes and unparseable URLs fail closed", () => {
    expect(
      resolveArtifactPolicy({ E2E_TRACE: "yes-please" }, LOCAL_APP, LOCAL_API)
        .trace,
    ).toBe("off");
    expect(isLocalUrl("not a url")).toBe(false);
    expect(isLocalUrl("http://localhost.evil.com")).toBe(false);
    expect(isLocalUrl("http://[::1]:8000")).toBe(true);
  });

  it("the committed config routes reporter + trace through the policy", () => {
    const cfg = readFileSync(
      path.resolve(__dirname, "../../playwright.config.ts"),
      "utf8",
    );
    expect(cfg).toContain("resolveArtifactPolicy(");
    expect(cfg).toMatch(/reporter:\s*ARTIFACTS\.reporter/);
    expect(cfg).toMatch(/trace:\s*ARTIFACTS\.trace/);
    expect(cfg).not.toMatch(/\[\s*["']html["']/);
  });

  it("frontend/.gitignore ignores reports, test results and storageState", () => {
    const gi = readFileSync(
      path.resolve(__dirname, "../../.gitignore"),
      "utf8",
    ).split("\n");
    for (const p of ["playwright-report/", "test-results/", ".auth/"]) {
      expect(gi).toContain(p);
    }
  });
});
