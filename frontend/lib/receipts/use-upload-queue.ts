"use client";

/**
 * Upload-queue hook — P5-T6.
 *
 * Owns the multi-file upload lifecycle for the receipts screen. Each file becomes
 * a queue item with its own state, a stable idempotency key (so a retry never
 * creates a duplicate), and a local blob preview URL of the bytes the user just
 * dropped (NFR-SEC4: we never construct an object-store URL).
 *
 * The backend is synchronous, so an upload resolves directly to the extracted
 * receipt — there is no polling. ocr_status='failed' surfaces as a retryable
 * error; ocr_status='processed' makes the item reviewable.
 */

import * as React from "react";
import type { Receipt } from "@/lib/types";
import { useUploadReceipt } from "@/lib/query/receipt-hooks";
import { isPdfFile, isUploadRejection, uploadErrorMessage } from "./validation";

export type QueueItemStatus = "uploading" | "processed" | "failed";

export interface QueueItem {
  id: string;
  file: File;
  fileName: string;
  idempotencyKey: string;
  status: QueueItemStatus;
  /** In-browser blob URL of the uploaded file (revoked on removal/unmount). */
  previewUrl: string;
  /** Present once status === 'processed'. */
  receipt?: Receipt;
  /** Present once status === 'failed'. */
  error?: string;
  /**
   * False when the server refused the file itself (too large, wrong type,
   * damaged, over quota): retrying the same file cannot help (phase 11).
   */
  retryable?: boolean;
}

export interface UseUploadQueueResult {
  items: QueueItem[];
  enqueue: (files: File[]) => void;
  retry: (id: string) => void;
  remove: (id: string) => void;
  selectedId: string | null;
  select: (id: string | null) => void;
  selectedItem: QueueItem | null;
  /** Reflect a server receipt back into its queue item (e.g. after a correction). */
  applyReceiptUpdate: (receipt: Receipt) => void;
}

function newId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `id-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function makePreviewUrl(file: File): string {
  // Phase 12: never preview a PDF locally — the server renders it to a PNG and
  // the review shows that image (the only form of a PDF the app displays).
  if (isPdfFile(file)) return "";
  try {
    return URL.createObjectURL(file);
  } catch {
    return "";
  }
}

function revokePreviewUrl(url: string): void {
  if (!url) return;
  try {
    URL.revokeObjectURL(url);
  } catch {
    /* no-op */
  }
}

export interface UseUploadQueueOptions {
  /** Called once per upload the server processed (e.g. to toast its outcome). */
  onProcessed?: (receipt: Receipt) => void;
}

export function useUploadQueue(
  options: UseUploadQueueOptions = {},
): UseUploadQueueResult {
  const [items, setItems] = React.useState<QueueItem[]>([]);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const uploadMutation = useUploadReceipt();
  // Latest callback without re-creating runUpload on every render.
  const onProcessedRef = React.useRef(options.onProcessed);
  onProcessedRef.current = options.onProcessed;

  // Track live blob URLs so we can revoke them all on unmount.
  const urlsRef = React.useRef<Set<string>>(new Set());
  React.useEffect(() => {
    const urls = urlsRef.current;
    return () => {
      urls.forEach(revokePreviewUrl);
      urls.clear();
    };
  }, []);

  const patchItem = React.useCallback(
    (id: string, patch: Partial<QueueItem>) => {
      setItems((prev) =>
        prev.map((it) => (it.id === id ? { ...it, ...patch } : it)),
      );
    },
    [],
  );

  const runUpload = React.useCallback(
    async (item: QueueItem) => {
      try {
        const receipt = await uploadMutation.mutateAsync({
          file: item.file,
          idempotencyKey: item.idempotencyKey,
        });
        if (receipt.ocr_status === "failed") {
          patchItem(item.id, {
            status: "failed",
            error: "Extraction failed. Please retry.",
            receipt,
          });
        } else {
          patchItem(item.id, {
            status: "processed",
            receipt,
            error: undefined,
          });
          setSelectedId((curr) => curr ?? item.id);
          onProcessedRef.current?.(receipt);
        }
      } catch (err) {
        patchItem(item.id, {
          status: "failed",
          error: uploadErrorMessage(err),
          retryable: !isUploadRejection(err),
        });
      }
    },
    [patchItem, uploadMutation],
  );

  const enqueue = React.useCallback(
    (files: File[]) => {
      const newItems: QueueItem[] = files.map((file) => {
        const previewUrl = makePreviewUrl(file);
        if (previewUrl) urlsRef.current.add(previewUrl);
        return {
          id: newId(),
          file,
          fileName: file.name,
          idempotencyKey: newId(),
          status: "uploading" as const,
          previewUrl,
        };
      });
      setItems((prev) => [...prev, ...newItems]);
      newItems.forEach((item) => void runUpload(item));
    },
    [runUpload],
  );

  const retry = React.useCallback(
    (id: string) => {
      setItems((prev) => {
        const item = prev.find((it) => it.id === id);
        if (item) {
          // Re-run with the SAME idempotency key → server dedupes, no duplicate.
          void runUpload({ ...item, status: "uploading" });
        }
        return prev.map((it) =>
          it.id === id ? { ...it, status: "uploading", error: undefined } : it,
        );
      });
    },
    [runUpload],
  );

  const remove = React.useCallback((id: string) => {
    setItems((prev) => {
      const item = prev.find((it) => it.id === id);
      if (item?.previewUrl) {
        revokePreviewUrl(item.previewUrl);
        urlsRef.current.delete(item.previewUrl);
      }
      return prev.filter((it) => it.id !== id);
    });
    setSelectedId((curr) => (curr === id ? null : curr));
  }, []);

  const applyReceiptUpdate = React.useCallback((receipt: Receipt) => {
    setItems((prev) =>
      prev.map((it) =>
        it.receipt?.id === receipt.id ? { ...it, receipt } : it,
      ),
    );
  }, []);

  const selectedItem = items.find((it) => it.id === selectedId) ?? null;

  return {
    items,
    enqueue,
    retry,
    remove,
    selectedId,
    select: setSelectedId,
    selectedItem,
    applyReceiptUpdate,
  };
}
