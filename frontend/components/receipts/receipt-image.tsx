"use client";

import * as React from "react";
import { useReceipt } from "@/lib/query/receipt-hooks";
import { ImagePreview } from "./image-preview";

/**
 * Scoped receipt image — C2 (NFR-SEC4).
 *
 * Fetches GET /receipts/{id}, which returns a short-lived signed
 * `image_access_url` only after the server's RBAC + record scoping admit the
 * caller (owner, owner's direct manager, finance). The query refetches before
 * the link expires (see useReceipt); if the <img> still fails to load (e.g. it
 * expired while the tab was hidden), it refetches ONCE for a fresh link — the
 * guard only re-arms after a successful load, so a permanently broken object
 * can never cause a refetch loop.
 *
 * When the server has no servable link (local storage, provider error, 404/403)
 * it falls back to `fallbackUrl` (e.g. the uploader's local blob preview) or the
 * existing "Preview unavailable" placeholder. The signed URL is never logged,
 * toasted or persisted.
 */

export interface ReceiptImageProps {
  receiptId: string;
  fileName: string;
  /** Shown when the server provides no usable link (e.g. a local blob URL). */
  fallbackUrl?: string;
  /** Phase 12: the image was rendered from an uploaded PDF (label only). */
  fromPdf?: boolean;
  className?: string;
}

export function ReceiptImage({
  receiptId,
  fileName,
  fallbackUrl = "",
  fromPdf,
  className,
}: ReceiptImageProps) {
  const { data, refetch } = useReceipt(receiptId);
  const serverUrl = data?.image_access_url ?? "";

  // One refetch per failure streak; re-armed by a successful load.
  const retriedRef = React.useRef(false);
  // A link that failed even after the retry — fall back rather than loop.
  const [failedUrl, setFailedUrl] = React.useState<string | null>(null);

  const showServer = serverUrl !== "" && serverUrl !== failedUrl;
  const url = showServer ? serverUrl : fallbackUrl;

  const handleError = React.useCallback(() => {
    if (!showServer) return;
    if (!retriedRef.current) {
      retriedRef.current = true;
      void refetch();
      return;
    }
    setFailedUrl(serverUrl);
  }, [showServer, serverUrl, refetch]);

  const handleLoad = React.useCallback(() => {
    if (showServer) retriedRef.current = false;
  }, [showServer]);

  return (
    <ImagePreview
      url={url}
      fileName={fileName}
      fromPdf={fromPdf}
      className={className}
      onError={handleError}
      onLoad={handleLoad}
    />
  );
}
