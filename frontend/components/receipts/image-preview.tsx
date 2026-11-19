"use client";

import * as React from "react";
import { ZoomIn, ZoomOut, RotateCw, ImageOff } from "lucide-react";
import { AtlasButton } from "@/components/atlas";
import { cn } from "@/lib/utils";
import { displayFileName } from "@/lib/receipts/display-name";

/**
 * Image preview — P5-T6.
 *
 * Renders the receipt image with zoom + rotate. The source is either the LOCAL
 * blob URL of the file the user just uploaded, or a short-lived signed link the
 * server minted after scoping (see ReceiptImage). NFR-SEC4: we never construct an
 * object-store URL; receipt.image_url is an opaque server key we never touch.
 * `onError` / `onLoad` let the caller refresh an expired signed link.
 *
 * Phase 12: it only ever renders an <img>. A PDF receipt is shown as the PNG
 * the server rendered from it (`fromPdf` adds a "from PDF" label); the PDF
 * itself is never embedded or served.
 */

export interface ImagePreviewProps {
  url: string;
  fileName: string;
  /** The image was rendered from an uploaded PDF (shows a "from PDF" label). */
  fromPdf?: boolean;
  className?: string;
  /** Fired when the <img> fails to load (e.g. an expired signed link). */
  onError?: () => void;
  /** Fired when the <img> loads successfully. */
  onLoad?: () => void;
}

const ZOOM_STEP = 0.25;
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;

export function ImagePreview({
  url,
  fileName: rawFileName,
  fromPdf,
  className,
  onError,
  onLoad,
}: ImagePreviewProps) {
  // Display only: a client file name may carry bidi / control characters.
  const fileName = displayFileName(rawFileName);
  const [zoom, setZoom] = React.useState(1);
  const [rotation, setRotation] = React.useState(0);

  const zoomIn = () => setZoom((z) => Math.min(ZOOM_MAX, z + ZOOM_STEP));
  const zoomOut = () => setZoom((z) => Math.max(ZOOM_MIN, z - ZOOM_STEP));
  const rotate = () => setRotation((r) => (r + 90) % 360);

  if (!url) {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center rounded-atlas-md border border-border bg-muted/40 p-[var(--space-8)] text-muted-foreground",
          className,
        )}
      >
        <ImageOff className="h-10 w-10" strokeWidth={1} aria-hidden="true" />
        <p className="atlas-text-caption mt-2">Preview unavailable</p>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex flex-col rounded-atlas-md border border-border bg-card overflow-hidden",
        className,
      )}
    >
      {/* Toolbar */}
      <div className="flex items-center justify-between gap-2 border-b border-border px-[var(--space-3)] py-[var(--space-2)]">
        <span className="flex min-w-0 items-center gap-2">
          <span
            className="atlas-text-caption text-muted-foreground truncate"
            title={fileName}
          >
            {fileName}
          </span>
          {fromPdf && (
            <span
              data-testid="from-pdf-badge"
              className="atlas-text-caption shrink-0 rounded-atlas-sm border border-border px-1.5 py-0.5 text-muted-foreground"
            >
              from PDF
            </span>
          )}
        </span>
        <div className="flex items-center gap-1 shrink-0">
          <AtlasButton
            size="sm"
            variant="ghost"
            onClick={zoomOut}
            disabled={zoom <= ZOOM_MIN}
            aria-label="Zoom out"
          >
            <ZoomOut className="h-4 w-4" aria-hidden="true" />
          </AtlasButton>
          <span className="atlas-text-caption text-muted-foreground tabular-nums w-10 text-center">
            {Math.round(zoom * 100)}%
          </span>
          <AtlasButton
            size="sm"
            variant="ghost"
            onClick={zoomIn}
            disabled={zoom >= ZOOM_MAX}
            aria-label="Zoom in"
          >
            <ZoomIn className="h-4 w-4" aria-hidden="true" />
          </AtlasButton>
          <AtlasButton
            size="sm"
            variant="ghost"
            onClick={rotate}
            aria-label="Rotate"
          >
            <RotateCw className="h-4 w-4" aria-hidden="true" />
          </AtlasButton>
        </div>
      </div>

      {/* Preview surface */}
      <div className="relative flex-1 overflow-auto bg-muted/30 p-[var(--space-4)] min-h-[280px]">
        <div className="flex min-h-full items-center justify-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt={`Receipt preview of ${fileName}`}
            onError={onError}
            onLoad={onLoad}
            className="max-w-full origin-center transition-transform duration-atlas-normal"
            style={{
              transform: `scale(${zoom}) rotate(${rotation}deg)`,
            }}
          />
        </div>
      </div>
    </div>
  );
}
