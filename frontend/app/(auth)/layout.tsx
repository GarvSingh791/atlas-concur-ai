import type { Metadata } from "next";

// Phase 12 follow-ups: reset / invite / verify links carry a one-time token in
// the URL; never send it to another origin in a Referer header.
export const metadata: Metadata = { referrer: "no-referrer" };

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen grid place-items-center bg-background px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <span className="text-2xl font-bold tracking-tight text-primary">
            Atlas
          </span>
        </div>
        {children}
        <p className="mt-8 text-center text-xs leading-relaxed text-muted-foreground">
          Independent engineering study inspired by SAP Concur — not affiliated
          with or endorsed by SAP. All data is synthetic.
        </p>
      </div>
    </div>
  );
}
