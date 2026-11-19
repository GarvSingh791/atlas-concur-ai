/**
 * P7-T4 global setup — fail FAST if the live stack is not ready.
 *
 * The E2E suite drives the real UI against a real FastAPI + Postgres + MinIO
 * backend (seeded, LLM_PROVIDER=local). Playwright's webServer boots the
 * frontend, but the backend + datastores are an external prerequisite it does
 * not manage. If either tier is down, every spec would fail with opaque
 * timeouts — so we probe both here and abort with a clear, actionable message.
 * No silent green on a dead stack.
 *
 * CORRECTION (verified live): the backend liveness route is the ROOT `/health`
 * (returns 200). `/api/v1/health` 404s — do not probe it.
 */

const BACKEND_HEALTH = "http://localhost:8000/health";
const FRONTEND_LOGIN = "http://localhost:3000/login";

async function probe(url: string, label: string): Promise<void> {
  let lastErr: unknown;
  // The frontend webServer may still be compiling on first run; give it a few
  // attempts before declaring the tier dead.
  for (let attempt = 1; attempt <= 30; attempt++) {
    try {
      const res = await fetch(url, { redirect: "manual" });
      // 200-399 (incl. Next's 200 on /login) all prove the tier is answering.
      if (res.status >= 200 && res.status < 400) return;
      lastErr = new Error(`${label} responded ${res.status} at ${url}`);
    } catch (err) {
      lastErr = err;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(
    `[P7-T4 globalSetup] ${label} is not reachable at ${url}.\n` +
      `  Bring up the stack before running the suite:\n` +
      `    docker compose -f docker/docker-compose.yml up -d   # postgres + minio + backend (seeded)\n` +
      `    (frontend boots automatically via Playwright webServer)\n` +
      `  Last error: ${String(lastErr)}`,
  );
}

export default async function globalSetup(): Promise<void> {
  await probe(BACKEND_HEALTH, "Backend (:8000)");
  await probe(FRONTEND_LOGIN, "Frontend (:3000)");
}
