"use client";

import { Menu, LogOut } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { MeResponse } from "@/lib/types/identity";
import { GlobalSearch } from "./global-search";
import { NotificationBell } from "./notification-bell";
import { useLogout } from "@/lib/query/auth-hooks";

interface TopBarProps {
  title: string;
  user: MeResponse | null;
  onMobileMenuToggle: () => void;
  showHamburger: boolean;
}

export function TopBar({
  title,
  user,
  onMobileMenuToggle,
  showHamburger,
}: TopBarProps) {
  const logout = useLogout();

  const roleLabel = user?.role
    ? user.role
        .split("_")
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ")
    : "User";

  const initials = user?.name
    ? user.name
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : "AT";

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4 md:px-6">
      <div className="flex items-center gap-4">
        {showHamburger && (
          <button
            onClick={onMobileMenuToggle}
            className="rounded-md p-1 -ml-1 text-muted-foreground hover:bg-secondary hover:text-foreground focus-visible:atlas-focus-ring"
            aria-label="Open navigation menu"
          >
            <Menu className="h-5 w-5" />
          </button>
        )}
        <h1 className="text-base font-semibold text-foreground tracking-tight line-clamp-1">
          {title}
        </h1>
      </div>

      <div className="flex items-center gap-2 md:gap-4">
        {/* Global search + notifications (product gap 3) */}
        <GlobalSearch />
        <NotificationBell />

        {/* Account Menu */}
        <DropdownMenu>
          <DropdownMenuTrigger className="rounded-full focus-visible:atlas-focus-ring outline-none">
            <Avatar className="h-8 w-8 hover:opacity-90 transition-opacity border border-border/50">
              <AvatarFallback className="bg-primary/10 text-primary text-xs font-medium">
                {initials}
              </AvatarFallback>
            </Avatar>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56" sideOffset={8}>
            <DropdownMenuLabel className="font-normal">
              <div className="flex flex-col space-y-1">
                <p className="text-sm font-medium leading-none">
                  {user?.name || "Atlas User"}
                </p>
                <p className="text-xs leading-none text-muted-foreground">
                  {user?.email || "user@example.com"}
                </p>
                <div className="mt-2">
                  <Badge
                    variant="secondary"
                    className="font-normal text-[10px] uppercase px-1.5 py-0"
                  >
                    {roleLabel}
                  </Badge>
                </div>
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={() => logout()}
              className="text-destructive focus:bg-destructive/10 focus:text-destructive cursor-pointer"
            >
              <LogOut className="mr-2 h-4 w-4" />
              <span>Sign out</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
