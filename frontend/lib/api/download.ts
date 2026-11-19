/**
 * Authenticated file download (Product gap 8 — CSV exports).
 *
 * The export endpoints need the bearer token, so a plain <a href> cannot be
 * used: fetch with the Authorization header, recover once from a 401 through
 * the shared single-flight refresh, then hand the blob to the browser.
 * Non-2xx → a typed ApiError (the caller shows an error toast). Never logs
 * tokens.
 */

import { recoverFromUnauthorized } from "@/lib/api/client";
import { getAccessToken } from "@/lib/auth/token-store";
import { ApiError } from "@/lib/types/api";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export type DownloadParams = Record<string, string | number | undefined | null>;

export function buildDownloadUrl(
  path: string,
  params?: DownloadParams,
): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v === undefined || v === null || String(v).trim() === "") continue;
    sp.set(k, String(v));
  }
  const qs = sp.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

/** Today as YYYY-MM-DD (local) — the fallback filename date. */
export function todayStamp(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

async function toApiError(res: Response): Promise<ApiError> {
  const correlationId = res.headers.get("X-Correlation-ID") ?? "unknown";
  try {
    const body = (await res.json()) as {
      error?: { code?: string; message?: string; correlation_id?: string };
    };
    return new ApiError(
      {
        code: body.error?.code ?? "NETWORK_ERROR",
        message: body.error?.message ?? `HTTP ${res.status}`,
        correlation_id: body.error?.correlation_id ?? correlationId,
      },
      res.status,
    );
  } catch {
    return new ApiError(
      {
        code: "NETWORK_ERROR",
        message: `HTTP ${res.status}`,
        correlation_id: correlationId,
      },
      res.status,
    );
  }
}

function send(url: string, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { Accept: "text/csv" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return fetch(url, { method: "GET", headers, credentials: "include" });
}

/** Fetch *path* with auth and save it as *filename*. Resolves when handed off. */
export async function downloadFile(
  path: string,
  params: DownloadParams | undefined,
  filename: string,
): Promise<void> {
  const url = buildDownloadUrl(path, params);
  const sent = getAccessToken();
  let res = await send(url, sent);
  if (res.status === 401) {
    const fresh = await recoverFromUnauthorized(sent);
    if (fresh === null) {
      throw new ApiError(
        {
          code: "UNAUTHORIZED",
          message: "Session expired. Please sign in again.",
          correlation_id: res.headers.get("X-Correlation-ID") ?? "unknown",
        },
        401,
      );
    }
    res = await send(url, fresh);
  }
  if (!res.ok) throw await toApiError(res);

  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the browser has picked the download up.
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
