import { AuthGuard } from "./auth-guard";
import { AppShell } from "@/components/atlas/app-shell";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      <AppShell>{children}</AppShell>
    </AuthGuard>
  );
}
