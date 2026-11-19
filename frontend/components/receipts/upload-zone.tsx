"use client";

import * as React from "react";
import { UploadCloud, Camera, FolderOpen } from "lucide-react";
import { AtlasButton, useAtlasToast } from "@/components/atlas";
import { cn } from "@/lib/utils";
import { validateReceiptFile, ACCEPT_ATTR } from "@/lib/receipts/validation";
import { displayFileName } from "@/lib/receipts/display-name";

/**
 * Upload zone — P5-T6.
 *
 * Drag-drop + Browse + Camera + multi-file. Validates type/size CLIENT-SIDE before
 * handing files upstream; invalid files are rejected with a clear message and never
 * enter the upload queue. This is input guarding only — the server inspects the
 * content and has the final say (phase 11).
 */

export interface UploadZoneProps {
  onFiles: (files: File[]) => void;
  className?: string;
}

export function UploadZone({ onFiles, className }: UploadZoneProps) {
  const [dragActive, setDragActive] = React.useState(false);
  const browseRef = React.useRef<HTMLInputElement>(null);
  const cameraRef = React.useRef<HTMLInputElement>(null);
  const { showToast } = useAtlasToast();

  const handleFiles = React.useCallback(
    (list: FileList | null) => {
      if (!list || list.length === 0) return;
      const valid: File[] = [];
      const errors: string[] = [];
      for (const file of Array.from(list)) {
        const result = validateReceiptFile(file);
        if (result.valid) valid.push(file);
        else errors.push(`${displayFileName(file.name)}: ${result.error}`);
      }
      if (errors.length > 0) {
        showToast({
          variant: "error",
          title:
            errors.length === 1
              ? "File rejected"
              : `${errors.length} files rejected`,
          description: errors.join(" · "),
        });
      }
      if (valid.length > 0) onFiles(valid);
    },
    [onFiles, showToast],
  );

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragActive(false);
    handleFiles(e.dataTransfer.files);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={(e) => {
        e.preventDefault();
        setDragActive(false);
      }}
      onDrop={onDrop}
      role="button"
      tabIndex={0}
      aria-label="Upload receipts — drag and drop, or use the buttons"
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          browseRef.current?.click();
        }
      }}
      className={cn(
        "flex flex-col items-center justify-center gap-[var(--space-3)] rounded-atlas-lg border-2 border-dashed px-[var(--space-6)] py-[var(--space-8)] text-center transition-colors duration-atlas-normal",
        dragActive
          ? "border-[var(--color-primary-600)] bg-[var(--color-primary-50)]"
          : "border-border bg-card hover:border-[var(--color-primary-400)] hover:bg-muted/40",
        className,
      )}
    >
      <div
        className={cn(
          "rounded-atlas-full p-[var(--space-3)] transition-colors duration-atlas-normal",
          dragActive
            ? "text-[var(--color-primary-600)]"
            : "text-muted-foreground",
        )}
      >
        <UploadCloud
          className="h-10 w-10"
          strokeWidth={1.5}
          aria-hidden="true"
        />
      </div>

      <div>
        <p className="atlas-text-subtitle font-medium text-foreground">
          {dragActive ? "Drop to upload" : "Drag & drop receipts here"}
        </p>
        <p className="atlas-text-caption text-muted-foreground mt-1">
          PNG, JPG or PDF · up to 10 MB · multiple files supported
        </p>
      </div>

      <div className="flex flex-wrap items-center justify-center gap-2">
        <AtlasButton
          variant="primary"
          size="md"
          onClick={() => browseRef.current?.click()}
        >
          <FolderOpen className="h-4 w-4 mr-1.5" aria-hidden="true" />
          Browse
        </AtlasButton>
        <AtlasButton
          variant="secondary"
          size="md"
          onClick={() => cameraRef.current?.click()}
        >
          <Camera className="h-4 w-4 mr-1.5" aria-hidden="true" />
          Camera
        </AtlasButton>
      </div>

      {/* Hidden inputs */}
      <input
        ref={browseRef}
        type="file"
        accept={ACCEPT_ATTR}
        multiple
        className="hidden"
        onChange={(e) => {
          handleFiles(e.target.files);
          e.target.value = "";
        }}
      />
      <input
        ref={cameraRef}
        type="file"
        accept="image/*"
        capture="environment"
        className="hidden"
        onChange={(e) => {
          handleFiles(e.target.files);
          e.target.value = "";
        }}
      />
    </div>
  );
}
