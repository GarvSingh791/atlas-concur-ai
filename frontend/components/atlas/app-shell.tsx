"use client";

import { useSession } from "@/lib/auth/session-provider";
import {
  canAccessPath,
  findNavItemForPath,
  getVisibleNavItems,
  getMobileTabItems,
} from "@/lib/nav-config";
import { ShieldOff } from "lucide-react";
import Link from "next/link";
import { useAppShell } from "@/lib/hooks/use-app-shell";
import { Sidebar } from "./sidebar";
import { TopBar } from "./top-bar";
import { MobileTabBar } from "./mobile-tab-bar";
import { AIFab } from "./ai-fab";
import { AssistantDrawer } from "./assistant-drawer";
import { DemoBanner } from "./demo-banner";
import { VerifyEmailBanner } from "./verify-email-banner";
import { usePathname } from "next/navigation";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, role, permissions } = useSession();
  const {
    sidebarMode,
    isMobile,
    isSidebarOpen,
    toggleSidebar,
    closeSidebar,
    isAssistantOpen,
    toggleAssistant,
    closeAssistant,
  } = useAppShell();
  const pathname = usePathname();

  // Navigation data based on session
  const visibleNavItems = getVisibleNavItems(permissions, role);
  const mobileNavItems = getMobileTabItems(permissions, role);

  // Derive current page title from the nav item that owns this route (exact or
  // sub-route), resolved against the full catalog so a gated page still shows
  // its own title rather than falling back to "Dashboard" (C7-26).
  const currentNavItem = findNavItemForPath(pathname);
  const title = currentNavItem?.label || "Dashboard";

  // C7-26: route-level role gate driven by the SAME nav-config mapping that
  // hides the item from the nav. Typing a hidden URL shows a clear no-access
  // state instead of the page. Server-side enforcement is unchanged.
  const hasAccess = canAccessPath(pathname, permissions, role);

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Sidebar (Desktop/Tablet & Mobile Drawer) */}
      <Sidebar
        items={visibleNavItems}
        role={role}
        mode={sidebarMode}
        isOpen={isSidebarOpen}
        onClose={closeSidebar}
        onAssistantToggle={toggleAssistant}
      />

      {/* Main Content Area */}
      <div className="flex flex-1 flex-col min-w-0 overflow-hidden">
        <TopBar
          title={title}
          user={user}
          onMobileMenuToggle={toggleSidebar}
          showHamburger={sidebarMode === "hidden"}
        />

        {/* Phase 11: shared public demo notice (DEMO_MODE only) */}
        <DemoBanner />
        {/* Phase 12 follow-ups: unverified email nudge (never a block) */}
        <VerifyEmailBanner />

        {/* Scrollable Page Content */}
        <main
          className="flex-1 overflow-y-auto p-4 md:p-6 pb-24 md:pb-6"
          id="main-content"
          tabIndex={-1}
        >
          {hasAccess ? children : <NoAccess />}
        </main>
      </div>

      {/* Assistant Surfaces */}
      <AssistantDrawer
        isOpen={isAssistantOpen}
        onClose={closeAssistant}
        isMobile={isMobile}
      />

      {/* FAB (Hidden on Mobile, replaced by Tab Bar) */}
      {!isMobile && (
        <AIFab isOpen={isAssistantOpen} onClick={toggleAssistant} />
      )}

      {/* Mobile Tab Bar */}
      {isMobile && (
        <MobileTabBar
          items={mobileNavItems}
          onAssistantToggle={toggleAssistant}
        />
      )}
    </div>
  );
}

function NoAccess() {
  return (
    <div
      role="alert"
      className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center"
    >
      <ShieldOff
        className="h-10 w-10 text-muted-foreground"
        aria-hidden="true"
      />
      <h2 className="text-lg font-semibold text-foreground">
        You don&apos;t have access to this page
      </h2>
      <p className="text-sm text-muted-foreground">
        Your role doesn&apos;t include this area of Atlas. If you think this is
        a mistake, contact your administrator.
      </p>
      <Link
        href="/dashboard"
        className="text-sm font-medium text-[var(--color-primary-600)] hover:underline"
      >
        Go to dashboard
      </Link>
    </div>
  );
}
