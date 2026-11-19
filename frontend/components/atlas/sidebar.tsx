"use client";

import { Compass, Sparkles, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { type NavItem } from "@/lib/nav-config";
import type { RoleKey } from "@/lib/types/identity";
import { type SidebarMode } from "@/lib/hooks/use-app-shell";

interface SidebarProps {
  items: NavItem[];
  role: RoleKey | null;
  mode: SidebarMode;
  isOpen?: boolean;
  onClose?: () => void;
  onAssistantToggle: () => void;
}

/**
 * CSS-only tooltip wrapper for collapsed sidebar items.
 * Avoids @radix-ui/react-compose-refs entirely — no refs, no setState in commit.
 */
function SidebarTooltip({
  label,
  children,
  enabled,
}: {
  label: string;
  children: React.ReactNode;
  enabled: boolean;
}) {
  if (!enabled) return <>{children}</>;

  return (
    <div className="relative group/tooltip">
      {children}
      <div
        role="tooltip"
        className={cn(
          "pointer-events-none absolute left-full top-1/2 z-50 ml-2 -translate-y-1/2",
          "whitespace-nowrap rounded-md border border-border bg-popover px-2.5 py-1 text-xs text-popover-foreground shadow-md",
          "opacity-0 scale-95 transition-all duration-150",
          "group-hover/tooltip:opacity-100 group-hover/tooltip:scale-100",
        )}
      >
        {label}
      </div>
    </div>
  );
}

export function Sidebar({
  items,
  role,
  mode,
  isOpen = false,
  onClose,
  onAssistantToggle,
}: SidebarProps) {
  const pathname = usePathname();

  const isMobileDrawer = mode === "hidden";
  const showDrawer = isMobileDrawer && isOpen;
  const isCollapsed = mode === "collapsed";

  const roleLabel = role
    ? role
        .split("_")
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ")
    : "";

  const content = (
    <div
      className={cn(
        "flex h-full flex-col bg-[var(--surface-sidebar)] text-slate-300 transition-all duration-[var(--transition-normal)]",
        isCollapsed ? "w-16" : "w-60",
      )}
    >
      {/* Brand Header */}
      <div
        className={cn(
          "flex h-14 shrink-0 items-center border-b border-slate-700/50",
          isCollapsed ? "justify-center px-0" : "px-4 justify-between",
        )}
      >
        <Link
          href="/dashboard"
          className="flex items-center gap-2 focus-visible:atlas-focus-ring rounded-md p-1"
        >
          <Compass className="h-6 w-6 text-[var(--color-primary-400)] shrink-0" />
          {!isCollapsed && (
            <span className="text-xl font-bold tracking-tight text-white">
              Atlas
            </span>
          )}
        </Link>
        {isMobileDrawer && onClose && (
          <button
            onClick={onClose}
            className="rounded-md p-2 hover:bg-slate-800 text-slate-400 hover:text-white"
            aria-label="Close menu"
          >
            <X className="h-5 w-5" />
          </button>
        )}
      </div>

      {/* Role Badge */}
      {!isCollapsed && role && (
        <div className="px-4 py-3">
          <Badge
            variant="outline"
            className="border-slate-700 bg-slate-800/50 text-slate-300 font-normal"
          >
            {roleLabel}
          </Badge>
        </div>
      )}

      {/* Nav List */}
      {/* overflow-x-hidden: overflow-y:auto alone forces overflow-x to auto,
          and the collapsed-mode tooltips (absolute left-full, always mounted)
          then produced a stray horizontal scrollbar. They were already
          clipped by this box, so hiding the x-overflow changes nothing else. */}
      <nav
        className="flex-1 overflow-y-auto overflow-x-hidden px-2 py-2"
        aria-label="Main Navigation"
      >
        {items.map((item, index) => {
          const isActive = pathname === item.href;
          const Icon = item.icon;
          const isNewSection =
            item.section &&
            (index === 0 || items[index - 1].section !== item.section);

          return (
            <div key={item.href}>
              {isNewSection && !isCollapsed && (
                <div className="mt-6 mb-2 px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {item.section}
                </div>
              )}
              {isNewSection && isCollapsed && index !== 0 && (
                <div className="mx-4 my-2 h-px bg-slate-700/50" />
              )}

              <SidebarTooltip label={item.label} enabled={isCollapsed}>
                <Link
                  href={item.href}
                  onClick={() => isMobileDrawer && onClose?.()}
                  className={cn(
                    "group relative flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors focus-visible:atlas-focus-ring",
                    isActive
                      ? "bg-[var(--surface-sidebar-active)] text-white"
                      : "text-slate-400 hover:bg-[var(--surface-sidebar-hover)] hover:text-slate-200",
                    isCollapsed && "justify-center px-0",
                  )}
                  aria-label={isCollapsed ? item.label : undefined}
                  aria-current={isActive ? "page" : undefined}
                >
                  {isActive && (
                    <div className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r bg-[var(--color-primary-600)]" />
                  )}
                  <Icon
                    className={cn(
                      "h-5 w-5 shrink-0 transition-colors",
                      isActive
                        ? "text-[var(--color-primary-400)]"
                        : "text-slate-500 group-hover:text-slate-300",
                    )}
                    aria-hidden="true"
                  />
                  {!isCollapsed && <span>{item.label}</span>}
                </Link>
              </SidebarTooltip>
            </div>
          );
        })}
      </nav>

      {/* Assistant Launcher (Pinned Bottom) */}
      <div className="shrink-0 border-t border-slate-700/50 p-2">
        <SidebarTooltip label="Atlas Assistant" enabled={isCollapsed}>
          <button
            onClick={() => {
              onAssistantToggle();
              if (isMobileDrawer) onClose?.();
            }}
            className={cn(
              "group relative flex h-10 w-full items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors focus-visible:atlas-focus-ring text-slate-400 hover:bg-[var(--surface-sidebar-hover)] hover:text-slate-200",
              isCollapsed && "justify-center px-0",
            )}
            aria-label="Toggle Atlas Assistant (Cmd/Ctrl + J)"
          >
            <Sparkles className="h-5 w-5 shrink-0 text-[var(--color-ai-primary)]" />
            {!isCollapsed && <span>Atlas Assistant</span>}
          </button>
        </SidebarTooltip>
      </div>
    </div>
  );

  // Mobile drawer wrapper
  if (isMobileDrawer) {
    return (
      <>
        {showDrawer && (
          <div
            className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm transition-opacity"
            onClick={onClose}
            aria-hidden="true"
          />
        )}
        <div
          className={cn(
            "fixed inset-y-0 left-0 z-50 transform transition-transform duration-[var(--transition-spring)]",
            showDrawer ? "translate-x-0" : "-translate-x-full",
          )}
        >
          {content}
        </div>
      </>
    );
  }

  // Desktop/Tablet inline sidebar
  return (
    <aside className="h-full border-r border-slate-800 z-30 relative shrink-0">
      {content}
    </aside>
  );
}
