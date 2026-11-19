import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { defineConfig } from "@playwright/test";

import {
  resolveAppApiUrl,
  resolveArtifactPolicy,
} from "./playwright.artifact-policy";

const BASE_URL = "http://localhost:3000";
// The API the APP will call: shell env, else the .env files `next dev` loads
// (Playwright itself never reads frontend/.env.local).
const API_URL = resolveAppApiUrl(process.env, (name) => {
  const file = path.join(__dirname, name);
  return existsSync(file) ? readFileSync(file, "utf8") : null;
});

// Reporters/traces capture tokens + the login password — see
// playwright.artifact-policy.ts. Default: list reporter, traces off; HTML/trace
// are opt-in (E2E_HTML_REPORT=1 / E2E_TRACE=<mode>) and only against local
// targets unless E2E_ALLOW_SENSITIVE_ARTIFACTS=1 is set explicitly. With
// artifacts on, a dev server we did not start (unknown env) is never reused.
const ARTIFACTS = resolveArtifactPolicy(process.env, BASE_URL, API_URL);

/**
 * Atlas E2E config.
 *
 * Two suites share this one config via `projects`:
 *
 *   legacy  — the P6-T1 Tier-B live-verification specs (auth-refresh / screens /
 *             state-actions). They do their own UI login, hold no storageState,
 *             and must keep passing byte-for-byte. Isolated by testIgnore so the
 *             p7t4 specs never run inside them.
 *
 *   p7t4    — the P7-T4 end-to-end suite. A `setup` project logs in each role
 *             once via the API and saves a per-role storageState (.auth/*.json);
 *             the three role projects depend on it and reuse that state, so each
 *             role logs in exactly once for the whole run.
 *
 * Both suites drive the running frontend (:3000) against the LIVE backend
 * (:8000, LLM_PROVIDER=local). `globalSetup` fails fast if either is down.
 * e2e/ is excluded from tsconfig/vitest — it is not part of the product build.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/.artifacts",
  globalSetup: "./e2e/p7t4/global-setup.ts",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: ARTIFACTS.reporter,
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 },
    headless: true,
    screenshot: "off",
    trace: ARTIFACTS.trace,
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    // ---- legacy P6-T1 Tier-B specs — unchanged, no storageState ----
    {
      name: "legacy",
      testDir: "./e2e",
      testIgnore: ["**/p7t4/**"],
    },

    // ---- P7-T4: log in each role once, save per-role storageState ----
    {
      name: "setup",
      testDir: "./e2e/p7t4",
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: "p7t4-employee",
      testDir: "./e2e/p7t4",
      dependencies: ["setup"],
      testMatch: [
        "employee-flow.spec.ts",
        "cross-role-handoff.spec.ts",
        "rbac-ui.spec.ts",
      ],
      use: { storageState: ".auth/employee.json" },
    },
    {
      name: "p7t4-manager",
      testDir: "./e2e/p7t4",
      dependencies: ["setup"],
      testMatch: ["manager-flow.spec.ts"],
      use: { storageState: ".auth/manager.json" },
    },
    {
      name: "p7t4-finance",
      testDir: "./e2e/p7t4",
      dependencies: ["setup"],
      testMatch: ["finance-flow.spec.ts"],
      use: { storageState: ".auth/finance.json" },
    },
  ],

  /**
   * webServer — bring up the app for the suite.
   *
   * Locally, the Dockerized backend already owns :8000 (healthy + seeded) and
   * the only thing we must boot is the frontend dev server; `reuseExistingServer`
   * (true when not in CI) makes Playwright SKIP any command whose port already
   * answers, so the backend entry is a no-op locally and there is no :8000
   * collision. In CI the frontend (and, if you wire it, the backend) is started
   * fresh. The frontend entry intentionally inherits the shell env so it picks up
   * NEXT_PUBLIC_API_BASE_URL from frontend/.env.local (→ :8000/api/v1).
   */
  webServer: [
    {
      command: "npm run dev",
      url: "http://localhost:3000/login",
      reuseExistingServer: !process.env.CI && ARTIFACTS.mayReuseServer,
      timeout: 120_000,
    },
  ],
});
