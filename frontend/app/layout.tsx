import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "@/styles/globals.css";
import { Toaster } from "@/components/ui/toaster";
import { RateLimitToaster } from "@/components/ui/rate-limit-toaster";
import { SessionProvider } from "@/lib/auth/session-provider";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Atlas — Travel & Expense Copilot",
  description:
    "AI-powered travel and expense management that captures, categorises, and routes spend intelligently — so employees review rather than enter, and finance closes faster.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <SessionProvider>{children}</SessionProvider>
        <Toaster />
        <RateLimitToaster />
      </body>
    </html>
  );
}
