import { Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";

interface AIFabProps {
  onClick: () => void;
  className?: string;
  isOpen: boolean;
}

export function AIFab({ onClick, className, isOpen }: AIFabProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "fixed bottom-6 right-6 z-40 flex h-14 w-14 items-center justify-center rounded-full text-white shadow-[0_8px_16px_rgba(124,58,237,0.25)] transition-transform hover:scale-105 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ai-primary)] focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "bg-gradient-to-br from-[var(--color-ai-gradient-start)] to-[var(--color-ai-gradient-end)]",
        isOpen && "scale-0 opacity-0 pointer-events-none", // Hide fab when drawer is open
        className,
      )}
      aria-label="Open Atlas Assistant (Cmd/Ctrl + J)"
      aria-expanded={isOpen}
    >
      <Sparkles className="h-6 w-6" aria-hidden="true" />
    </button>
  );
}
