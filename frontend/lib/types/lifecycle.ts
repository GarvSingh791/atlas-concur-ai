/**
 * Report & trip lifecycle DTOs (product gaps 1, 4, 8) — mirrors
 * backend/app/modules/lifecycle/schemas.py.
 */

/** One APPROVED report finance may mark reimbursed (GET /reimbursements/queue). */
export interface ReimbursementQueueItem {
  id: string;
  title: string;
  owner_id: string;
  owner_name: string | null;
  /** Decimal serialized as a string. */
  total_amount: string;
  currency: string;
  submitted_at: string | null;
  approval_status: string | null;
  version: number;
  /** False for the caller's OWN report (segregation of duties). */
  can_reimburse: boolean;
}

export interface ReimbursementQueue {
  items: ReimbursementQueueItem[];
  total: number;
}
