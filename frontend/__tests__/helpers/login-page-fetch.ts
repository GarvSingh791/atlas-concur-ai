/**
 * A fetch stub for tests that render the sign-in page.
 *
 * The page makes two background requests besides the sign-in call itself: a
 * credential-free GET /health (cold-start warm-up) and GET /public/config (the
 * demo organisation's login code). Both are answered here so the test's own
 * mock only sees the API calls it scripts. Pass `passHealth` to let /health
 * reach the mock (to assert on the ping), and `demoOrgCode` to simulate a demo
 * deployment.
 */
export function loginPageFetch(
  api: typeof fetch,
  opts: { passHealth?: boolean; demoOrgCode?: string | null } = {},
): typeof fetch {
  const demoOrgCode = opts.demoOrgCode ?? null;
  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health") && !opts.passHealth) {
      return Promise.resolve(new Response(null));
    }
    if (url.endsWith("/public/config")) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            demo_mode: demoOrgCode !== null,
            demo_reset: {
              enabled: false,
              interval_hours: null,
              next_reset_at: null,
              last_reset_at: null,
            },
            demo_org_code: demoOrgCode,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    }
    return api(input, init);
  }) as typeof fetch;
}
