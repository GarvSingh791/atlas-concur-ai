import { Toaster } from "@/components/ui/toaster";

/**
 * Dev route group layout — standalone, no shell, no auth.
 * Used for the component gallery and other development tools.
 */
export default function DevLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-[var(--surface-background)]">
      {children}
      <Toaster />
    </div>
  );
}
