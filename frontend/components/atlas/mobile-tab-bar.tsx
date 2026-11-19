import Link from "next/link";
import { usePathname } from "next/navigation";
import { Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";
import { type NavItem } from "@/lib/nav-config";

interface MobileTabBarProps {
  items: NavItem[];
  onAssistantToggle: () => void;
}

export function MobileTabBar({ items, onAssistantToggle }: MobileTabBarProps) {
  const pathname = usePathname();

  return (
    <nav className="fixed bottom-0 left-0 right-0 z-40 flex h-16 items-center justify-around border-t border-border bg-card pb-safe px-2 sm:hidden">
      {items.map((item) => {
        const isActive = pathname === item.href;
        const Icon = item.icon;

        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex flex-1 flex-col items-center justify-center gap-1 py-1 rounded-md transition-colors",
              isActive
                ? "text-[var(--color-primary-600)]"
                : "text-muted-foreground hover:text-foreground",
            )}
            aria-current={isActive ? "page" : undefined}
          >
            <div className="relative">
              <Icon className="h-5 w-5" aria-hidden="true" />
              {isActive && (
                <div className="absolute -top-2 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full bg-[var(--color-primary-600)]" />
              )}
            </div>
            <span className="text-[10px] font-medium leading-none">
              {item.label}
            </span>
          </Link>
        );
      })}

      {/* Assistant Launcher Tab */}
      <button
        onClick={onAssistantToggle}
        className="flex flex-1 flex-col items-center justify-center gap-1 py-1 rounded-md text-[var(--color-ai-primary)] hover:text-[var(--color-ai-primary)]/80 transition-colors"
        aria-label="Open Assistant"
      >
        <Sparkles className="h-5 w-5" aria-hidden="true" />
        <span className="text-[10px] font-medium leading-none">Assistant</span>
      </button>
    </nav>
  );
}
