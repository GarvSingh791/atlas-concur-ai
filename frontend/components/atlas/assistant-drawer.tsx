import { X, Sparkles } from "lucide-react";
import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { AssistantPanel } from "./assistant/assistant-panel";

interface AssistantDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  isMobile: boolean;
}

export function AssistantDrawer({
  isOpen,
  onClose,
  isMobile,
}: AssistantDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);

  // Focus trap and esc key are handled by useAppShell for esc, but we should focus the drawer when it opens
  useEffect(() => {
    if (isOpen && drawerRef.current) {
      drawerRef.current.focus();
    }
  }, [isOpen]);

  if (!isOpen && !isMobile) {
    // We still render it invisible on desktop for animations, but on mobile we might unmount it
    // Actually, let's keep it mounted for transitions
  }

  return (
    <>
      {/* Overlay — dims main content (~60%) and dismisses on click-outside.
          Rendered on both desktop and mobile when open. */}
      {isOpen && (
        <div
          className={cn(
            "fixed inset-0 z-40 bg-black/40 transition-opacity",
            isMobile && "backdrop-blur-sm",
          )}
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      {/* Drawer */}
      <div
        ref={drawerRef}
        tabIndex={-1}
        role="dialog"
        aria-label="Atlas Assistant"
        className={cn(
          "fixed z-50 flex flex-col bg-[var(--color-ai-surface)] shadow-2xl transition-transform duration-[var(--transition-spring)] outline-none",
          // Mobile: Full screen overlay from bottom
          isMobile
            ? "inset-x-0 bottom-0 top-14 rounded-t-2xl"
            : "bottom-4 right-4 top-20 w-[400px] rounded-2xl border border-border/50",
          isOpen
            ? "translate-y-0 md:translate-x-0"
            : "translate-y-full md:translate-x-[120%] md:translate-y-0",
        )}
      >
        {/* Header */}
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-[var(--color-primary-100)] px-4 bg-white/50 rounded-t-2xl">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--color-ai-primary)]/10">
              <Sparkles className="h-4 w-4 text-[var(--color-ai-primary)]" />
            </div>
            <h2 className="text-sm font-semibold text-foreground">
              Atlas Assistant
            </h2>
          </div>
          <button
            onClick={onClose}
            className="rounded-full p-2 text-muted-foreground hover:bg-black/5 hover:text-foreground transition-colors focus-visible:atlas-focus-ring"
            aria-label="Close assistant"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Conversational surface (P5-T10) */}
        <div className="flex flex-1 flex-col overflow-hidden">
          <AssistantPanel isOpen={isOpen} />
        </div>
      </div>
    </>
  );
}
