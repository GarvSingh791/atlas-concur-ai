/**
 * Expense write errors → toast copy (C7R UAT).
 *
 * One mapping for every expense mutation (entry edit, rename, move, create) so
 * a stale `If-Match` version always reads the same way as the submit bar's:
 * the report changed since it was opened — no silent overwrite.
 */

import type { AtlasToastOptions } from "@/components/atlas/atlas-toast";
import { isApiError } from "@/lib/types/api";

export function expenseErrorToast(
  err: unknown,
  title: string,
): AtlasToastOptions {
  if (isApiError(err) && err.code === "CONFLICT") {
    return {
      variant: "error",
      title: "This report changed",
      description:
        "It was updated since you opened it (or is no longer a draft). The latest version has been loaded — review it and try again.",
    };
  }
  return {
    variant: "error",
    title,
    description: isApiError(err)
      ? err.message
      : "Something went wrong. Please try again.",
  };
}
