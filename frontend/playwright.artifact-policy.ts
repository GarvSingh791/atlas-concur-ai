/**
 * Playwright artifact policy — which reporters / traces a run may produce.
 *
 * WHY THIS EXISTS: Playwright's HTML reporter and traces capture every request
 * and response, including the login POST body (demo password), the
 * Authorization headers (live access tokens) and the storageState files
 * (.auth/*.json, live refresh tokens). A report generated against a deployed
 * backend is therefore a credential dump. So:
 *
 *   - the default is the `list` reporter only and traces OFF;
 *   - the HTML reporter (E2E_HTML_REPORT=1) and traces (E2E_TRACE=<mode>) are
 *     honoured only when BOTH the app and the API are local (localhost /
 *     127.0.0.1 / ::1), unless the operator explicitly accepts the risk with
 *     E2E_ALLOW_SENSITIVE_ARTIFACTS=1;
 *   - the API URL checked is the one the APP will use: the shell env first,
 *     then the same .env files `next dev` loads (.env.development.local,
 *     .env.local, .env.development, .env) — not just the Playwright process
 *     env, which never sees .env.local (resolveAppApiUrl);
 *   - when sensitive artifacts are on WITHOUT the explicit override, an
 *     already-running dev server is NOT reused (`reuseExistingServer: false`):
 *     its env cannot be inspected, so Playwright must start the server whose
 *     env was just checked (or fail fast because :3000 is taken);
 *   - wherever an HTML report does get written, it is opened "never" and its
 *     output dir is git-ignored (frontend/.gitignore).
 *
 * Limits (not enforceable from a config file): CLI flags such as
 * `--reporter=html` / `--trace on` override the config. The .gitignore entries
 * are the backstop for those; never run them against a deployed backend.
 *
 * Pure functions (no @playwright/test / fs import) so vitest can pin the policy.
 */

export type ReporterSpec =
  | ["list"]
  | ["html", { open: "never"; outputFolder: string }];

export type TraceMode =
  | "off"
  | "on"
  | "retain-on-failure"
  | "on-first-retry"
  | "on-all-retries";

export interface ArtifactPolicy {
  reporter: ReporterSpec[];
  trace: TraceMode;
  /** True when both targets are loopback hosts. */
  targetIsLocal: boolean;
  /** True when an HTML report or a trace will be written. */
  sensitive: boolean;
  /** Whether Playwright may reuse a dev server it did not start (and whose env
   *  it therefore could not check). False whenever artifacts are sensitive and
   *  only the locality check — not the explicit override — allowed them. */
  mayReuseServer: boolean;
}

/** Files `next dev` loads, highest precedence first (shell env beats all). */
export const NEXT_DEV_ENV_FILES = [
  ".env.development.local",
  ".env.local",
  ".env.development",
  ".env",
] as const;

/** The app's fallback when NEXT_PUBLIC_API_BASE_URL is unset (lib/api/client). */
export const DEFAULT_API_URL = "http://localhost:8000/api/v1";

/** Value of `key` in dotenv text, or undefined. `export ` and quotes allowed. */
export function readDotenvValue(text: string, key: string): string | undefined {
  let found: string | undefined;
  for (const line of text.split(/\r?\n/)) {
    const m = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(
      line,
    );
    if (!m || m[1] !== key) continue;
    let v = m[2].trim();
    const q = v[0];
    if ((q === '"' || q === "'" || q === "`") && v.lastIndexOf(q) > 0) {
      v = v.slice(1, v.lastIndexOf(q));
    } else {
      v = v.replace(/\s+#.*$/, "").trim();
    }
    found = v; // dotenv: the last assignment in a file wins
  }
  return found;
}

/**
 * The API base URL the dev server will hand the browser, resolved the way
 * Next does: process env first, then NEXT_DEV_ENV_FILES in order. A value using
 * `$VAR` expansion cannot be resolved here, so it returns "" (never local —
 * fail closed). `readFile` returns the file text or null when absent.
 */
export function resolveAppApiUrl(
  env: Record<string, string | undefined>,
  readFile: (name: string) => string | null,
): string {
  const fromShell = env.NEXT_PUBLIC_API_BASE_URL;
  const pick = (v: string | undefined) =>
    v !== undefined && v.includes("$") ? "" : v;
  if (fromShell !== undefined) return pick(fromShell) ?? "";
  for (const name of NEXT_DEV_ENV_FILES) {
    const text = readFile(name);
    if (text === null) continue;
    const v = readDotenvValue(text, "NEXT_PUBLIC_API_BASE_URL");
    if (v !== undefined) return pick(v) ?? "";
  }
  return DEFAULT_API_URL;
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const TRACE_MODES: TraceMode[] = [
  "on",
  "retain-on-failure",
  "on-first-retry",
  "on-all-retries",
];

/** True only for a parseable URL whose host is a loopback name/address. */
export function isLocalUrl(url: string | undefined): boolean {
  if (!url) return false;
  try {
    return LOCAL_HOSTS.has(new URL(url).hostname);
  } catch {
    return false; // unparseable → treat as remote (fail closed)
  }
}

export function resolveArtifactPolicy(
  env: Record<string, string | undefined>,
  baseURL: string,
  apiURL: string,
): ArtifactPolicy {
  const targetIsLocal = isLocalUrl(baseURL) && isLocalUrl(apiURL);
  const override = env.E2E_ALLOW_SENSITIVE_ARTIFACTS === "1";
  const allowed = targetIsLocal || override;

  const reporter: ReporterSpec[] = [["list"]];
  if (allowed && env.E2E_HTML_REPORT === "1") {
    reporter.push([
      "html",
      { open: "never", outputFolder: "playwright-report" },
    ]);
  }

  const requested = env.E2E_TRACE as TraceMode | undefined;
  const trace: TraceMode =
    allowed && requested && TRACE_MODES.includes(requested) ? requested : "off";

  const sensitive = reporter.length > 1 || trace !== "off";
  return {
    reporter,
    trace,
    targetIsLocal,
    sensitive,
    mayReuseServer: !sensitive || override,
  };
}
