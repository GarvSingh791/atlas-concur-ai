/**
 * Card feed DTO mirrors — product gap 8.
 *
 * Mirrors backend/app/modules/card/schemas.py (GET /card-transactions, the
 * caller's OWN card swipes). Money is a Decimal string on the wire.
 */

import type { AmountConversion } from "@/lib/money/fx";

export type CardTransactionStatus = "matched" | "unmatched";

export interface CardTransaction {
  id: string;
  merchant: string | null;
  txn_date: string;
  amount: string;
  currency: string;
  /** Last four digits of the card only. */
  last_four: string | null;
  status: CardTransactionStatus;
  /** Matched entry / its report / its first receipt — only on the caller's own report. */
  expense_id: string | null;
  report_id: string | null;
  receipt_id: string | null;
  amount_conversion?: AmountConversion | null;
}

export interface CardTransactionListParams {
  status?: CardTransactionStatus;
  date_from?: string;
  date_to?: string;
  page?: number;
  size?: number;
}
