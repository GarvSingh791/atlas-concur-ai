import { useState, useEffect, useCallback } from "react";
import { usePathname } from "next/navigation";
import { useMediaQuery } from "./use-media-query";
import { useAssistantStore } from "@/stores/assistant-store";
import { deriveContextFromPath } from "@/lib/assistant/context";

export type SidebarMode = "expanded" | "collapsed" | "hidden";

/**
 * Central state management for the App Shell.
 *
 * Responsibilities:
 * - Derive sidebar mode from window width
 * - Manage mobile/tablet sidebar drawer open state
 * - Manage assistant drawer open state
 * - Register global keyboard shortcut (Cmd/Ctrl + J) for assistant
 */
export function useAppShell() {
  // Breakpoints from spec
  // isDesktop (>= 1280px) is the default when both isTablet and isMobile are false
  const isTablet = useMediaQuery("(min-width: 1024px) and (max-width: 1279px)"); // lg: collapsed 64px
  const isMobile = useMediaQuery("(max-width: 1023px)"); // md & below: hidden, hamburger, tab bar

  const pathname = usePathname();

  // Component state
  const [isSidebarOpen, setIsSidebarOpen] = useState(false); // For hamburger menu
  // Assistant open state lives in the global store so any screen can open it
  // (and so the conversation survives navigation). The shell stays the owner
  // of the FAB/launcher/hotkey wiring built in P5-T3.
  const isAssistantOpen = useAssistantStore((s) => s.isOpen);

  // Derived state
  let sidebarMode: SidebarMode = "expanded";
  if (isTablet) sidebarMode = "collapsed";
  if (isMobile) sidebarMode = "hidden";

  // Actions
  const toggleSidebar = useCallback(
    () => setIsSidebarOpen((prev) => !prev),
    [],
  );
  const closeSidebar = useCallback(() => setIsSidebarOpen(false), []);

  const toggleAssistant = useCallback(() => {
    const store = useAssistantStore.getState();
    // When opening via FAB/launcher/hotkey, scope to the current route's
    // trip/report (no-op for global routes). Explicit openScoped() from a
    // screen bypasses this path, so it is never overwritten.
    if (!store.isOpen) {
      store.openWithContext(deriveContextFromPath(pathname));
    }
    store.toggle();
  }, [pathname]);

  const closeAssistant = useCallback(
    () => useAssistantStore.getState().close(),
    [],
  );

  // Global Keyboard Shortcut: Cmd/Ctrl + J toggles assistant
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        toggleAssistant();
      }
      if (e.key === "Escape" && isAssistantOpen) {
        closeAssistant();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleAssistant, closeAssistant, isAssistantOpen]);

  // Close drawers when resizing past breakpoints
  useEffect(() => {
    if (!isMobile) {
      setIsSidebarOpen(false);
    }
    // Only re-run when the mobile breakpoint changes, not when sidebar state changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile]);

  return {
    sidebarMode,
    isMobile,
    isSidebarOpen,
    toggleSidebar,
    closeSidebar,
    isAssistantOpen,
    toggleAssistant,
    closeAssistant,
  };
}
