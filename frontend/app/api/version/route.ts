/**
 * GET /api/version — the git commit this frontend build was made from (P8-T4).
 *
 * CI's deploy-staging job polls this until `commit` equals the pushed SHA, which
 * proves Vercel has promoted the NEW build to the production domain (not just
 * that the old one still answers). Vercel exposes VERCEL_GIT_COMMIT_SHA at build
 * time ("Automatically expose System Environment Variables" is on); anywhere
 * else (local dev, the CI Docker image) the value is "local".
 *
 * force-static: the SHA is fixed per build, so the response is computed once at
 * build time and served from the deployment's own cache. Public information —
 * the commit of a public repo — and the route needs no auth (auth is a
 * client-side session guard in app/(app); there is no middleware to bypass).
 */
import { NextResponse } from "next/server";

export const dynamic = "force-static";

export function GET(): NextResponse {
  return NextResponse.json({
    commit: process.env.VERCEL_GIT_COMMIT_SHA || "local",
  });
}
