"use client";

/**
 * AuthGuard — client-side protected-route boundary for the (app) group.
 *
 * Reads session status from the Zustand store:
 *   'loading'        → render LoadingState (prevents flash of protected content)
 *   'unauthenticated'→ redirect to /login
 *   'authenticated'  → render children
 *
 * WHY client-side guard (not Next.js middleware)?
 * Tokens are stored in memory (access) and localStorage (refresh) — not in
 * cookies — so middleware cannot read them server-side. A client guard is the
 * correct pattern for this token-storage approach. If tokens move to httpOnly
 * cookies in P6, the guard can be lifted to middleware.
 *
 * Fail-closed: 'loading' shows a spinner, never the protected content.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { useAuthStore, selectStatus } from "@/stores/auth-store";

interface AuthGuardProps {
  children: React.ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps) {
  const status = useAuthStore(selectStatus);
  const router = useRouter();

  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace("/login");
    }
  }, [status, router]);

  if (status === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <Loader2
          className="h-8 w-8 animate-spin text-primary"
          aria-label="Loading Atlas…"
        />
      </div>
    );
  }

  if (status === "unauthenticated") {
    // Show nothing while the redirect fires
    return null;
  }

  return <>{children}</>;
}
