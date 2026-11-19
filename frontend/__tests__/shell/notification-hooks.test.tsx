import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/client", () => ({ api: { get: apiGet, post: vi.fn() } }));

import {
  NOTIFICATION_POLL_MS,
  notificationKeys,
  useUnreadCount,
} from "@/lib/query/notification-hooks";

afterEach(cleanup);

describe("useUnreadCount", () => {
  it("polls every 60 s and refetches on window focus (overriding the app default)", async () => {
    apiGet.mockResolvedValue({ unread: 2 });
    const qc = new QueryClient({
      defaultOptions: { queries: { refetchOnWindowFocus: false } },
    });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useUnreadCount(), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual({ unread: 2 }));
    expect(apiGet).toHaveBeenCalledWith("/notifications/unread-count");
    const query = qc
      .getQueryCache()
      .find({ queryKey: notificationKeys.unread() });
    const opts = query?.options as {
      refetchInterval?: number;
      refetchOnWindowFocus?: boolean;
    };
    expect(NOTIFICATION_POLL_MS).toBe(60_000);
    expect(opts.refetchInterval).toBe(60_000);
    expect(opts.refetchOnWindowFocus).toBe(true);
  });
});
