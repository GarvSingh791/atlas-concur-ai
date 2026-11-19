"use client";

import { CheckCircle2, XCircle, Info } from "lucide-react";
import { useToast } from "@/lib/hooks/use-toast";

/**
 * Atlas Toast — convenience wrappers that trigger the existing Radix toast
 * with Atlas-specific icons and styling.
 *
 * Spec: UI_UX.md → Toasts
 * Success / error / info; slide-in 300ms; auto-dismiss 4s; non-blocking.
 */

export type ToastVariant = "success" | "error" | "info";

const TOAST_CONFIG: Record<
  ToastVariant,
  {
    icon: React.ElementType;
    iconColor: string;
  }
> = {
  success: {
    icon: CheckCircle2,
    iconColor: "var(--color-success)",
  },
  error: {
    icon: XCircle,
    iconColor: "var(--color-error)",
  },
  info: {
    icon: Info,
    iconColor: "var(--color-info)",
  },
};

export interface AtlasToastOptions {
  /** Toast variant. */
  variant: ToastVariant;
  /** Toast title. */
  title: string;
  /** Optional description. */
  description?: string;
}

/**
 * Hook that returns a `showToast` function for Atlas-styled toasts.
 *
 * Usage:
 * ```tsx
 * const { showToast } = useAtlasToast();
 * showToast({ variant: "success", title: "Report submitted" });
 * ```
 */
export function useAtlasToast() {
  const { toast } = useToast();

  const showToast = ({ variant, title, description }: AtlasToastOptions) => {
    const config = TOAST_CONFIG[variant];
    const Icon = config.icon;

    toast({
      title: (
        <span className="flex items-center gap-2">
          <Icon
            className="h-5 w-5 shrink-0"
            style={{ color: config.iconColor }}
            aria-hidden="true"
          />
          {title}
        </span>
      ) as unknown as string,
      description,
      duration: 4000,
    });
  };

  return { showToast };
}
