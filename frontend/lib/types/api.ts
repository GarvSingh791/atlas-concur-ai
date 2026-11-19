/**
 * Shared API contract types — the foundation layer.
 *
 * Success responses: bare Pydantic model (no wrapper)
 * Error responses:  { error: { code, message, correlation_id } }
 * Paginated:        { items, total, page, size, pages }
 *
 * These are read-only mirrors of the backend contract — never infer from docs,
 * always reconcile to actual backend source.
 */

// ---------------------------------------------------------------------------
// Error envelope (backend: app/core/errors.py)
// ---------------------------------------------------------------------------

export interface ApiErrorDetail {
  /** Machine-readable error code, e.g. "UNAUTHORIZED", "NOT_FOUND". */
  code: string;
  /** Safe, human-readable message. Never includes server internals. */
  message: string;
  /** Correlation ID for tracing — matches X-Correlation-ID header. */
  correlation_id: string;
  /** 429 only (code RATE_LIMITED): seconds to wait before trying again. */
  retry_after_seconds?: number;
}

export interface ApiErrorEnvelope {
  error: ApiErrorDetail;
}

/** Known backend error codes from ErrorCode class in core/errors.py. */
export const API_ERROR_CODES = {
  INTERNAL: "INTERNAL_SERVER_ERROR",
  VALIDATION: "VALIDATION_ERROR",
  NOT_FOUND: "NOT_FOUND",
  UNAUTHORIZED: "UNAUTHORIZED",
  FORBIDDEN: "FORBIDDEN",
  BAD_REQUEST: "BAD_REQUEST",
  CONFLICT: "CONFLICT",
  METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
  NOT_ACCEPTABLE: "NOT_ACCEPTABLE",
  PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
  UNSUPPORTED_MEDIA_TYPE: "UNSUPPORTED_MEDIA_TYPE",
  TOO_MANY_REQUESTS: "TOO_MANY_REQUESTS",
  /** Phase 11 request limiter (app/core/rate_limit.py): HTTP 429 with
   * `retry_after_seconds` + Retry-After. Never retried automatically. */
  RATE_LIMITED: "RATE_LIMITED",
  /** Approval module (app/modules/approval/service.py): the subject is not
   * (or is no longer) awaiting an approval decision. */
  INVALID_SUBJECT_STATE: "INVALID_SUBJECT_STATE",
  /** Follow-up (grades): the approval needs an approver of a higher grade
   * (app/modules/approval/grade_routing.py) — HTTP 403. */
  INSUFFICIENT_APPROVER_GRADE: "INSUFFICIENT_APPROVER_GRADE",
  /** Policy rule authoring (app/modules/policy/router.py, Issue 5): the
   * requested rule can't be expressed by the rule engine. */
  RULE_NOT_EXPRESSIBLE: "RULE_NOT_EXPRESSIBLE",
  /** The AI faithfulness check rejected the draft; the message carries why. */
  RULE_CRITIC_REJECTED: "RULE_CRITIC_REJECTED",
  /** The drafted (or to-be-activated) rule definition is not enforceable as
   * written; the message carries the validation reason. */
  RULE_INVALID: "RULE_INVALID",
  /** The language-model provider behind the policy assistant is unreachable. */
  PROVIDER_UNAVAILABLE: "PROVIDER_UNAVAILABLE",
} as const;

export type ApiErrorCode =
  (typeof API_ERROR_CODES)[keyof typeof API_ERROR_CODES];

/**
 * Typed API error — thrown by the client on non-2xx responses.
 *
 * Always parse via `isApiError()` before accessing `.code` / `.correlationId`.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly correlationId: string;
  readonly status: number;
  /** Seconds to wait before retrying (429 only). */
  readonly retryAfterSeconds?: number;

  constructor(detail: ApiErrorDetail, status: number) {
    super(detail.message);
    this.name = "ApiError";
    this.code = detail.code;
    this.correlationId = detail.correlation_id;
    this.status = status;
    if (typeof detail.retry_after_seconds === "number") {
      this.retryAfterSeconds = detail.retry_after_seconds;
    }
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/**
 * QA round: the server's own message for a request it rejected as invalid
 * (400 / 422 — e.g. "'submitted_from' must not be after 'submitted_to'."), so
 * a filter screen can say what is wrong instead of "Something went wrong".
 * Null for anything else (5xx, network, auth) — those keep the generic copy.
 */
export function validationErrorMessage(err: unknown): string | null {
  if (!isApiError(err)) return null;
  if (err.status !== 400 && err.status !== 422) return null;
  const message = err.message?.trim();
  return message ? message : null;
}

/**
 * A request that never got an HTTP response (DNS / TCP / TLS / CORS failure,
 * offline). Thrown by the API client in place of fetch's bare TypeError so the
 * retry policy can tell a transport failure from a TypeError bug in app code.
 * Extends TypeError so existing `instanceof TypeError` checks keep working.
 */
export class NetworkError extends TypeError {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "NetworkError";
  }
}

export function isNetworkError(err: unknown): err is NetworkError {
  return err instanceof NetworkError;
}

// ---------------------------------------------------------------------------
// Paginated response (backend: app/core/pagination.py Page[T])
// ---------------------------------------------------------------------------

/**
 * Standard paginated collection response.
 * Maps exactly to backend Page[T] model.
 */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

// ---------------------------------------------------------------------------
// Query params
// ---------------------------------------------------------------------------

/** Standard pagination query params (1-based page). */
export interface PaginationParams {
  page?: number;
  size?: number;
}

/** Sort spec: "field:asc", "-field" (desc), or "field" (asc). */
export interface SortParams {
  sort?: string;
}
