"use client";

/**
 * Public, unauthenticated client configuration — phase 11.
 *
 * Wired to GET /public/config (backend/app/api/public_config.py): non-secret,
 * deployment-wide flags only: whether this is the shared public demo, and
 * (phase 12) the scheduled demo reset — on/off, interval, next / last reset.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";

/** Scheduled demo reset (phase 12). Times are ISO 8601 UTC. */
export interface DemoResetSchedule {
  enabled: boolean;
  interval_hours: number | null;
  next_reset_at: string | null;
  last_reset_at: string | null;
}

export interface PublicConfig {
  demo_mode: boolean;
  /** Absent on servers older than phase 12. */
  demo_reset?: DemoResetSchedule;
  /**
   * The demo organisation's login code, published by the server in demo mode only.
   * The sign-in page sends it so a demo sign-in verifies one password hash
   * instead of five (about 9 s on the free instance). Absent otherwise.
   */
  demo_org_code?: string | null;
}

export const publicConfigKeys = {
  config: () => ["public-config"] as const,
};

export function usePublicConfig() {
  return useQuery({
    queryKey: publicConfigKeys.config(),
    queryFn: () => api.get<PublicConfig>("/public/config", { skipAuth: true }),
    staleTime: 30 * 60 * 1000,
    retry: false,
  });
}
