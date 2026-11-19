/**
 * C7R issue 1 — Ask Policy / Assistant SSE stream must survive access-token
 * expiry. A 401 on the initial POST (before any frame) refreshes the access
 * token through the SAME single-flight refresh the JSON client uses, retries
 * the POST once, and on refresh failure follows the session-expired path
 * (tokens cleared, redirect to /login). Nothing is retried once the stream
 * has started emitting frames.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { streamMessages, type StreamCallbacks } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import {
  clearAllTokens,
  getAccessToken,
  getRefreshToken,
  setAccessToken,
  setRefreshToken,
} from "@/lib/auth/token-store";

const fetchMock = vi.fn();
const replaceMock = vi.fn();
const originalLocation = window.location;

function sseResponse(frames: string[]): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(f));
      c.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const json = (status: number, data: unknown) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

type Call = { url: string; auth: string | undefined };
function calls(): Call[] {
  return fetchMock.mock.calls.map(([url, init]) => ({
    url: String(url),
    auth: (init?.headers as Record<string, string> | undefined)?.[
      "Authorization"
    ],
  }));
}

function run(convId = "c1") {
  const events: Array<[string, string]> = [];
  let resolve!: (r: { ok: boolean; error?: Error }) => void;
  const done = new Promise<{ ok: boolean; error?: Error }>((r) => {
    resolve = r;
  });
  const cb: StreamCallbacks = {
    onMessage: () => {},
    onEvent: (e, d) => events.push([e, d]),
    onError: (error) => resolve({ ok: false, error }),
    onComplete: () => resolve({ ok: true }),
  };
  streamMessages(convId, { content: "hi" }, cb);
  return { events, done };
}

beforeEach(() => {
  localStorage.clear();
  clearAllTokens();
  fetchMock.mockReset();
  replaceMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, replace: replaceMock },
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

describe("C7R-1 streamMessages refreshes on 401", () => {
  it("401 → refresh → retries once with the new token and streams", async () => {
    setAccessToken("expired-at");
    setRefreshToken("good-rt");
    fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
      if (url.endsWith("/auth/refresh"))
        return json(200, { access_token: "new-at" });
      const auth = (init.headers as Record<string, string>)["Authorization"];
      if (auth === "Bearer expired-at") return json(401, { detail: "expired" });
      return sseResponse(['event: done\ndata: {"message_id":"m1"}\n\n']);
    });

    const { events, done } = run();
    const r = await done;
    expect(r.ok).toBe(true);
    expect(events).toEqual([["done", '{"message_id":"m1"}']]);
    const c = calls();
    expect(c).toHaveLength(3);
    expect(c[0].auth).toBe("Bearer expired-at");
    expect(c[1].url).toMatch(/\/auth\/refresh$/);
    expect(c[2].url).toMatch(/\/conversations\/c1\/messages$/);
    expect(c[2].auth).toBe("Bearer new-at");
    expect(getAccessToken()).toBe("new-at");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("concurrent 401s (stream + JSON client) share ONE refresh", async () => {
    setAccessToken("expired-at");
    setRefreshToken("good-rt");
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((r) => {
      releaseRefresh = r;
    });
    fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
      if (url.endsWith("/auth/refresh")) {
        await refreshGate;
        return json(200, { access_token: "new-at" });
      }
      const auth = (init.headers as Record<string, string>)["Authorization"];
      if (auth === "Bearer expired-at") return json(401, { detail: "expired" });
      if (url.includes("/conversations/"))
        return sseResponse(['event: done\ndata: {"message_id":"m"}\n\n']);
      return json(200, { ok: true });
    });

    const a = run("a");
    const b = run("b");
    const j = api.get<{ ok: boolean }>("/me");
    // let all three 401s land before the refresh resolves
    await new Promise((r) => setTimeout(r, 10));
    releaseRefresh();
    const [ra, rb, rj] = await Promise.all([a.done, b.done, j]);
    expect(ra.ok).toBe(true);
    expect(rb.ok).toBe(true);
    expect(rj).toEqual({ ok: true });
    expect(calls().filter((c) => c.url.endsWith("/auth/refresh"))).toHaveLength(
      1,
    );
  });

  it("refresh rejected → session-expired path: tokens cleared + /login", async () => {
    setAccessToken("expired-at");
    setRefreshToken("stale-rt");
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/auth/refresh")) return json(401, { detail: "nope" });
      return json(401, { detail: "expired" });
    });

    const { events, done } = run();
    const r = await done;
    expect(r.ok).toBe(false);
    expect(r.error?.message).toMatch(/session expired/i);
    expect(r.error?.message).not.toMatch(/HTTP 401/);
    expect(events).toEqual([]);
    expect(getRefreshToken()).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(replaceMock).toHaveBeenCalledWith("/login");
    // original POST + refresh; no retry of the message POST
    expect(calls()).toHaveLength(2);
  });

  it("still 401 after a successful refresh → no third attempt, session expired", async () => {
    setAccessToken("expired-at");
    setRefreshToken("good-rt");
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/auth/refresh"))
        return json(200, { access_token: "new-at" });
      return json(401, { detail: "user disabled" });
    });
    const r = await run().done;
    expect(r.ok).toBe(false);
    expect(r.error?.message).toMatch(/session expired/i);
    expect(calls()).toHaveLength(3);
    expect(replaceMock).toHaveBeenCalledWith("/login");
  });

  it("non-401 errors are reported unchanged and never trigger a refresh", async () => {
    setAccessToken("at");
    setRefreshToken("rt");
    fetchMock.mockResolvedValue(json(500, { detail: "boom" }));
    const r = await run().done;
    expect(r.ok).toBe(false);
    expect(r.error?.message).toMatch(/Stream failed: HTTP 500/);
    expect(calls()).toHaveLength(1);
  });

  it("never retries once the stream body has started emitting frames", async () => {
    setAccessToken("at");
    setRefreshToken("rt");
    const enc = new TextEncoder();
    let pulls = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(c) {
        pulls += 1;
        if (pulls === 1)
          c.enqueue(enc.encode('event: status\ndata: {"route":"policy"}\n\n'));
        else c.error(new TypeError("network dropped"));
      },
    });
    fetchMock.mockResolvedValue(new Response(body, { status: 200 }));
    const { events, done } = run();
    const r = await done;
    expect(r.ok).toBe(false);
    expect(events).toEqual([["status", '{"route":"policy"}']]);
    expect(calls()).toHaveLength(1);
  });
});
