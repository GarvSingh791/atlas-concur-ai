"use client";

import * as React from "react";
import { ExternalLink } from "lucide-react";

import { AtlasButton } from "@/components/atlas";
import { ReceiptImage } from "@/components/receipts/receipt-image";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useReceipt } from "@/lib/query/receipt-hooks";
import { displayText } from "@/lib/receipts/display-name";

/**
 * Open one receipt of an entry — UAT2 defect H.
 *
 * The image comes from the existing scoped preview (ReceiptImage → GET
 * /receipts/{id}, which re-applies RBAC + record scoping and mints a short-lived
 * signed link, NFR-SEC4). The same query backs the "Open in new tab" link, so it
 * always points at the current, unexpired link; with no servable link (e.g.
 * local storage) the preview says so and no link is offered. The signed URL is
 * never logged or persisted. Mounted only while open, so no link is minted for
 * receipts nobody looks at.
 */

export interface ReceiptViewerDialogProps {
  receiptId: string;
  /** e.g. "Receipt 2". */
  title: string;
  /**
   * e.g. "Hilton Chicago · Jun 13, 2026 · $900.00"; null when the backend sent
   * no per-receipt reading (older backend).
   */
  summary: string | null;
  onClose: () => void;
}

export function ReceiptViewerDialog({
  receiptId,
  title,
  summary,
  onClose,
}: ReceiptViewerDialogProps) {
  const { data } = useReceipt(receiptId);
  const url = data?.image_access_url ?? null;
  const fromPdf = data?.source_format === "pdf";

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] overflow-y-auto rounded-atlas-xl p-[var(--space-6)] shadow-atlas-3 sm:max-w-[720px]">
        <DialogHeader>
          <DialogTitle className="atlas-text-title break-words text-foreground">
            {displayText(title)}
          </DialogTitle>
          <DialogDescription className="atlas-text-body break-words text-muted-foreground">
            {summary
              ? displayText(summary)
              : "The receipt image, opened through a short-lived link."}
          </DialogDescription>
        </DialogHeader>
        <ReceiptImage
          receiptId={receiptId}
          fileName={title}
          fromPdf={fromPdf}
          className="min-w-0"
        />
        <DialogFooter className="gap-2">
          {url && (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="atlas-text-body inline-flex items-center justify-center gap-1.5 rounded-atlas-md px-3 py-2 font-medium text-[var(--color-primary-700)] underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ExternalLink className="h-4 w-4" aria-hidden="true" />
              Open in new tab
            </a>
          )}
          <AtlasButton
            type="button"
            variant="secondary"
            size="sm"
            onClick={onClose}
          >
            Close
          </AtlasButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
