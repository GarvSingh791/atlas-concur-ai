/**
 * SSE streaming helper — transport-only for P5-T2.
 *
 * Covers POST /api/v1/conversations/{id}/messages (SSE).
 * Uses fetch + ReadableStream (not EventSource — POST requires this).
 * The full UI consumer (message rendering, state management) is P5-T10.
 *
 * P6 hardening note: if the backend adds chunked transfer with
 * reconnect semantics, add Last-Event-ID support here.
 *
 * Auth (C7R): the access token lives 15 minutes. A 401 on the initial POST —
 * i.e. BEFORE any SSE frame has been read — triggers the same single-flight
 * refresh the JSON client uses (recoverFromUnauthorized), then the POST is
 * retried exactly once with the new token. The auth dependency rejects the
 * request before the handler runs, so the retry cannot double-submit a turn.
 * If the refresh fails the session-expired path runs (tokens cleared, redirect
 * to /login). Once the body has started streaming nothing is ever retried.
 */

import {
  parseRateLimited,
  recoverFromUnauthorized,
  redirectToLogin,
  SESSION_EXPIRED_MESSAGE,
} from "@/lib/api/client";
import { getAccessToken } from "@/lib/auth/token-store";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export interface StreamCallbacks {
  /** Called for each SSE data line received. */
  onMessage: (data: string) => void;
  /** Called for each SSE event+data pair, when an event: line precedes the data: line. */
  onEvent?: (event: string, data: string) => void;
  /** Called on network or parse error. */
  onError: (err: Error) => void;
  /** Called when the stream closes cleanly. */
  onComplete: () => void;
}

/**
 * Stream messages for a conversation.
 *
 * POST /api/v1/conversations/{conversationId}/messages
 * Returns a cancel function — call it to abort the stream.
 *
 * @example
 * const cancel = streamMessages("conv-uuid", { message: "Hello" }, {
 *   onMessage: (chunk) => appendToUI(chunk),
 *   onError: (err) => showError(err.message),
 *   onComplete: () => setStreamDone(),
 * });
 * // To abort: cancel()
 */
export function streamMessages(
  conversationId: string,
  body: Record<string, unknown>,
  callbacks: StreamCallbacks,
): () => void {
  const controller = new AbortController();

  const url = `${API_BASE}/conversations/${conversationId}/messages`;
  const payload = JSON.stringify(body);

  const post = (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    return fetch(url, {
      method: "POST",
      headers,
      body: payload,
      signal: controller.signal,
      credentials: "include",
    });
  };

  (async () => {
    try {
      const sentToken = getAccessToken();
      let res = await post(sentToken);

      // 401 before the body started → refresh once (shared single-flight) and
      // retry the POST once. Never retried after frames have been emitted.
      if (res.status === 401) {
        // Release the rejected response's connection before retrying.
        void res.body?.cancel().catch(() => {});
        const fresh = await recoverFromUnauthorized(sentToken);
        if (controller.signal.aborted) return;
        if (fresh === null) {
          callbacks.onError(new Error(SESSION_EXPIRED_MESSAGE));
          return;
        }
        res = await post(fresh);
        if (res.status === 401) {
          // Still rejected with a freshly minted token → session is dead
          // (mirrors apiFetch: clear tokens + /login).
          void res.body?.cancel().catch(() => {});
          redirectToLogin();
          callbacks.onError(new Error(SESSION_EXPIRED_MESSAGE));
          return;
        }
      }

      if (res.status === 429) {
        // Phase 11: throttled before any frame — a typed ApiError with the
        // wait (and the global countdown toast). Never retried here.
        callbacks.onError(await parseRateLimited(res));
        return;
      }

      if (!res.ok || !res.body) {
        callbacks.onError(
          new Error(`Stream failed: HTTP ${res.status} ${res.statusText}`),
        );
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let currentEvent = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? ""; // keep incomplete line

        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed === "") {
            // Blank line = frame boundary; reset current event
            currentEvent = "";
          } else if (trimmed.startsWith("event:")) {
            currentEvent = trimmed.slice(6).trim();
          } else if (trimmed.startsWith("data:")) {
            const data = trimmed.slice(5).trim();
            if (data && data !== "[DONE]") {
              callbacks.onMessage(data);
              if (currentEvent && callbacks.onEvent) {
                callbacks.onEvent(currentEvent, data);
              }
            }
          }
        }
      }

      callbacks.onComplete();
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") {
        return; // cancelled by caller — not an error
      }
      callbacks.onError(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  // Return cancel function
  return () => controller.abort();
}
