"use client";

import * as React from "react";
import { Receipt as ReceiptIcon, Loader2 } from "lucide-react";
import { EmptyState, ErrorState, Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { useDeleteReceipt } from "@/lib/query/receipt-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import { useUploadQueue } from "@/lib/receipts/use-upload-queue";
import { displayFileName } from "@/lib/receipts/display-name";
import { UploadZone } from "./upload-zone";
import { ReceiptQueue } from "./receipt-queue";
import { ExtractionReview, uploadOutcomeToast } from "./extraction-review";
import { CardFeed } from "./card-feed";

type ReceiptsTab = "uploads" | "cards";

/**
 * Receipts screen — P5-T6 orchestrator.
 *
 * Composes the upload zone + multi-file queue + extraction-review panel and owns
 * the screen's state machine: empty → uploading/extracting → review → error. The
 * backend is synchronous (no polling); the queue hook drives uploads and the
 * review panel renders the pipeline's truth and persists inline corrections.
 */

function ExtractingState() {
  return (
    <div className="flex flex-col items-center justify-center rounded-atlas-md border border-border bg-card py-[var(--space-12)] text-center">
      <Loader2
        className="h-8 w-8 animate-spin text-[var(--color-primary-600)]"
        aria-hidden="true"
      />
      <p className="atlas-text-subtitle font-medium text-foreground mt-[var(--space-4)]">
        Extracting…
      </p>
      <p className="atlas-text-caption text-muted-foreground mt-1">
        Reading your receipt and filling in the details.
      </p>
    </div>
  );
}

export function ReceiptsScreen() {
  const { showToast } = useAtlasToast();
  const {
    items,
    enqueue,
    retry,
    remove,
    selectedId,
    select,
    selectedItem,
    applyReceiptUpdate,
  } = useUploadQueue({
    // UAT2 defect D: a re-upload of an already-claimed receipt is announced as
    // it lands — never left behind an "In policy" notice nobody opened.
    onProcessed: (receipt) => {
      const toast = uploadOutcomeToast(receipt);
      if (toast) showToast(toast);
    },
  });

  // Product gap 8: the card feed lives in the Receipts area as a second tab.
  const [tab, setTab] = React.useState<ReceiptsTab>("uploads");
  const anyUploading = items.some((it) => it.status === "uploading");
  // UAT2: the X on a processed upload is a real delete (confirmed in-page).
  const [deleteId, setDeleteId] = React.useState<string | null>(null);
  const deleteItem = items.find((it) => it.id === deleteId) ?? null;
  const del = useDeleteReceipt();

  const confirmDelete = () => {
    const receiptId = deleteItem?.receipt?.id;
    if (!deleteItem || !receiptId || del.isPending) return;
    const itemId = deleteItem.id;
    del.mutate(
      { receiptId, discardDraftedEntry: true },
      {
        onSuccess: (res) => {
          remove(itemId);
          setDeleteId(null);
          showToast({
            variant: "success",
            title: "Receipt deleted",
            description:
              res.outcome === "deleted_with_entry"
                ? "The expense it created was removed too."
                : "The expense it backed now needs a receipt.",
          });
        },
        onError: (err) => {
          setDeleteId(null);
          showToast(expenseErrorToast(err, "Couldn’t delete the receipt"));
        },
      },
    );
  };

  const renderReview = () => {
    if (selectedItem?.status === "processed" && selectedItem.receipt) {
      return (
        <ExtractionReview
          receipt={selectedItem.receipt}
          previewUrl={selectedItem.previewUrl}
          fileName={selectedItem.fileName}
          onReceiptUpdate={applyReceiptUpdate}
        />
      );
    }
    if (selectedItem?.status === "uploading") {
      return <ExtractingState />;
    }
    if (selectedItem?.status === "failed") {
      return (
        <ErrorState
          heading="Extraction failed"
          subtext={selectedItem.error ?? "We couldn't read this receipt."}
          onRetry={() => retry(selectedItem.id)}
          secondaryLabel="Remove"
          onSecondary={() => remove(selectedItem.id)}
        />
      );
    }
    if (anyUploading) {
      return <ExtractingState />;
    }
    return (
      <EmptyState
        icon={ReceiptIcon}
        heading="Select a receipt"
        subtext="Pick a processed receipt from the queue to review its extracted fields."
      />
    );
  };

  return (
    <div className="flex flex-col gap-[var(--space-6)]">
      <header>
        <h1 className="atlas-text-heading text-foreground">Receipts</h1>
        <p className="atlas-text-body text-muted-foreground mt-1">
          Upload a receipt and we&apos;ll extract its details automatically.
          Review and correct any field before it&apos;s used.
        </p>
      </header>

      <div
        role="tablist"
        aria-label="Receipts views"
        className="flex gap-1 border-b border-border"
      >
        {(
          [
            ["uploads", "Uploads"],
            ["cards", "Card feed"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={`receipts-tab-${key}`}
            aria-selected={tab === key}
            aria-controls={`receipts-panel-${key}`}
            data-testid={`receipts-tab-${key}`}
            onClick={() => setTab(key)}
            className={
              tab === key
                ? "-mb-px border-b-2 border-[var(--color-primary-600)] px-3 py-2 atlas-text-body font-medium text-foreground"
                : "-mb-px border-b-2 border-transparent px-3 py-2 atlas-text-body text-muted-foreground hover:text-foreground"
            }
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "cards" ? (
        <div
          role="tabpanel"
          id="receipts-panel-cards"
          aria-labelledby="receipts-tab-cards"
        >
          <CardFeed onUploadReceipt={() => setTab("uploads")} />
        </div>
      ) : items.length === 0 ? (
        <div className="mx-auto w-full max-w-2xl">
          <UploadZone onFiles={enqueue} />
          <div className="mt-[var(--space-4)]">
            <EmptyState
              icon={ReceiptIcon}
              // This list shows only this session's uploads (there is no
              // receipts list endpoint yet), so don't claim there are none.
              heading="No uploads in this session"
              subtext="Upload a receipt to get started — drag a file above, browse, or use your camera. Expenses drafted from earlier uploads are in your Receipt Inbox under Expenses."
            />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-[var(--space-6)] lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
          <aside className="flex flex-col gap-[var(--space-4)]">
            <UploadZone onFiles={enqueue} />
            <ReceiptQueue
              items={items}
              selectedId={selectedId}
              onSelect={select}
              onRetry={retry}
              onRemove={remove}
              onDelete={setDeleteId}
            />
          </aside>
          <section className="min-w-0">{renderReview()}</section>
        </div>
      )}

      <Modal
        variant="destructive"
        open={deleteItem !== null}
        onOpenChange={(open) => !open && setDeleteId(null)}
        title="Delete this receipt?"
        description={`“${deleteItem ? displayFileName(deleteItem.fileName) : "This receipt"}” will be deleted. If it created a draft expense with no other receipt, that expense is removed too; otherwise the expense stays and will need a receipt. A receipt that was part of a submitted report is kept for audit. This can’t be undone.`}
        confirmLabel={del.isPending ? "Deleting…" : "Delete receipt"}
        onConfirm={confirmDelete}
      />
    </div>
  );
}
