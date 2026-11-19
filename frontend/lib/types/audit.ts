/**
 * Audit-log DTO types — mirrors backend/app/modules/audit/schemas.py
 * (AuditLogItem) and the allow-listed query surface of GET /api/v1/audit
 * (backend/app/modules/audit/service.py). Update both when the schema changes.
 *
 * The audit log is append-only and read-only over HTTP: there are no write
 * DTOs. actor_id / actor_name are null for system-initiated events.
 */

export interface AuditLogItem {
  id: string;
  /** Dotted event name, e.g. "trip.created". */
  action: string;
  /** Affected entity type, e.g. "trip". */
  entity_type: string;
  entity_id: string;
  actor_id: string | null;
  /** Resolved server-side; null for system-initiated events. */
  actor_name: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  /** ISO-8601 timestamp. */
  created_at: string;
}

/**
 * Query params for GET /audit. Filters are exact-match and allow-listed
 * server-side (action, entity_type, entity_id, actor_id); sort accepts
 * `created_at` only ("created_at:asc|desc" or "-created_at").
 */
export interface AuditListParams {
  page?: number;
  size?: number;
  sort?: string;
  action?: string;
  entity_type?: string;
  entity_id?: string;
  actor_id?: string;
}
