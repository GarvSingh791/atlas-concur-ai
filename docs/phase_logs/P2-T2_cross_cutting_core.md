# Phase Summary

Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T2 — Cross-cutting Core
Date:       2026-06-07

---

## Objective

Implement the consistent error contract + global exception handlers, a
per-request correlation ID bound to structured JSON logging, generic
pagination/filter/sort helpers + a paginated response envelope, the audit
interface (the single write path to `audit_log`), and the `audit_log` model.
Validate that a forced error returns the standard envelope with a correlation ID,
and that an audited test action writes exactly one `audit_log` row.

---

## Work Completed

**Reconciliation finding (the reason scope was narrow):**
Codebase inspection before writing a single line revealed that three of the four
P2-T2 pillars were already built and wired into the app factory in a prior build
(files dated the same day):

- **Error contract + global handlers** — `backend/app/core/errors.py` already
  defined the `{"error": {code, message, correlation_id}}` envelope, the
  `ErrorCode` constants, and the three handlers (422 / HTTPException / unhandled
  500). All three were already registered in `create_app()`.
- **Correlation-ID middleware** — `backend/app/core/correlation.py` was already a
  pure ASGI middleware storing the ID in both the ASGI scope and a ContextVar,
  echoing it on every response.
- **Structured JSON logging** — `backend/app/core/logging.py` already implemented
  `_JSONFormatter` (correlation ID on every line), `_CorrelationFilter`, and
  `setup_logging()`, called at app startup.

These were **reused as-is** — not rebuilt. The only genuinely-missing pieces were
added:

1. **`AppError` application-exception hierarchy** — base `AppError` carrying HTTP
   status + machine code + safe client-facing message, plus `BadRequestError`
   (400), `UnauthorizedError` (401), `ForbiddenError` (403), `NotFoundError`
   (404). Rendered through the existing private `_envelope()` helper so the
   envelope shape is identical across all error paths. The matching
   `app_error_handler` was added and registered in `create_app()`.

2. **`CreatedAtMixin`** — a single-responsibility mixin providing `created_at`
   (TIMESTAMPTZ NOT NULL, server default `now()`) only. Added to `mixins.py`
   alongside the existing P2-T1 mixins, keeping one-convention-per-mixin
   discipline. `TimestampMixin` (both columns) was left untouched.

3. **`audit_log` model** — exact schema per `DATABASE_SCHEMA.md`:
   `UUIDPrimaryKeyMixin + TenantMixin + CreatedAtMixin + Base`. Columns:
   `id`, `organization_id`, `actor_id` (nullable — NULL for system-initiated
   events), `action`, `entity_type`, `entity_id`, `before_state` (JSONB
   nullable), `after_state` (JSONB nullable), `created_at`. No `updated_at`,
   `version`, or `deleted_at` — append-only by design (NFR-R4).
   `organization_id` and `actor_id` are plain UUID columns; FK references to
   `organizations` and `users` are deferred to P3-T1 when those tables exist
   (same pattern P2-T1 used for mixin FKs). Composite index
   `ix_audit_log_entity_type_entity_id_created_at` per `DATABASE_SCHEMA.md`.

4. **`audit.py` — the single write path** (Dev Rule 8) — `record_audit(session,
   *, organization_id, action, entity_type, entity_id, actor_id=None,
   before_state=None, after_state=None) -> AuditLog`. Inserts and flushes
   (surfaces DB errors immediately, populates server-side `id`/`created_at`)
   without committing — the caller's transaction owns the commit, ensuring the
   audit row is atomic with the business mutation it records.

5. **`pagination.py`** — `clamp_pagination()`, `PaginationParams` (with `.offset`
   and `.limit` derivations), a `paginated()` FastAPI dependency, and `Page[T]`
   generic envelope (`items, total, page, size, pages`). `parse_sort()` accepts
   `field:asc|desc` and `-field` notation; rejects unknown fields and invalid
   directions via `BadRequestError`. `parse_filters()` rejects any key not in the
   caller-supplied allow-set via `BadRequestError`. Both helpers are
   deny-by-default: arbitrary client input is never passed through.

6. **`X-Request-ID` alias** — correlation middleware extended to also accept
   inbound `X-Request-ID` (common gateway/proxy header). `X-Correlation-ID` wins
   when both are present; `X-Correlation-ID` remains the canonical response
   header. The send-wrapper changed from `append` to `set` to fix a pre-existing
   latent duplicate-header bug (see QA findings).

7. **Three Validate-gate test files** — `test_error_contract.py` (hermetic),
   `test_audit.py` (live Postgres, `@pytest.mark.db`, self-skips in CI),
   `test_pagination.py` (hermetic).

8. **`DEFAULT_PAGE_SIZE` (20) and `MAX_PAGE_SIZE` (100)** added to `Settings`
   (config-driven per Dev Rule 12).

---

## Files Created

| File | Purpose |
|------|---------|
| `backend/app/core/audit.py` | Single append-only write path for `audit_log` |
| `backend/app/core/pagination.py` | Pagination params, Page envelope, safe sort/filter parsers |
| `database/models/audit_log.py` | `AuditLog` ORM model — exact schema, FKs deferred |
| `backend/tests/test_error_contract.py` | Validate gate: error envelope + correlation ID propagation |
| `backend/tests/test_audit.py` | Validate gate: one `audit_log` row per audited action |
| `backend/tests/test_pagination.py` | Unit tests for pagination helpers (hermetic) |
| `docs/phase_logs/P2-T2_cross_cutting_core.md` | This file |

---

## Files Modified

| File | Change |
|------|--------|
| `backend/app/core/errors.py` | Added `AppError` base + four subclasses + `app_error_handler`; all render through the existing `_envelope()` helper |
| `backend/app/main_entry/app.py` | Imported and registered `app_error_handler` for `AppError` |
| `backend/app/core/correlation.py` | Added `X-Request-ID` inbound alias; changed send-wrapper from `append` to `set` (duplicate-header fix) |
| `database/models/mixins.py` | Added `CreatedAtMixin` (created_at only, standalone — `TimestampMixin` untouched) |
| `database/models/__init__.py` | Exported `AuditLog` and `CreatedAtMixin` |
| `config/settings/base.py` | Added `DEFAULT_PAGE_SIZE: int = 20` and `MAX_PAGE_SIZE: int = 100` |

---

## Integration Points

- **`record_audit` ↔ P2-T1 session** — the audit interface accepts a `Session`
  from `get_db()` / `get_sessionmaker()` (the P2-T1 sync SQLAlchemy session
  pattern) and writes through it without committing. This makes the audit row
  atomic with the surrounding business mutation.
- **`AppError` ↔ existing error envelope** — `app_error_handler` calls the private
  `_envelope()` helper defined in P2-T1's `errors.py`, so the envelope shape
  (`{"error": {code, message, correlation_id}}`) is identical across all four
  error paths (422, HTTPException, AppError, unhandled 500).
- **`AuditLog` ↔ P2-T1 Base/mixins** — the model inherits `UUIDPrimaryKeyMixin`
  and `TenantMixin` from P2-T1's mixin library; `CreatedAtMixin` is new but
  follows the same one-column-one-mixin pattern.
- **`AuditLog` FK seam (P3-T1)** — `organization_id` and `actor_id` are plain UUID
  columns now. When `organizations` and `users` tables land in P3-T1, the FK
  constraints are wired as `ForeignKey(...)` on these columns — the column types
  and the index are already in place.
- **`parse_sort` / `parse_filters` ↔ `BadRequestError`** — helper rejection flows
  through the new `AppError` hierarchy (which is already registered in the app
  factory), so domain endpoints get the standard 400 envelope for free.

---

## Technical Decisions

1. **Logging config left in `core/logging.py` (noted deviation)** — The
   `MASTER_EXECUTION_PLAN` Section 3 places logging *configuration*
   (formatters, levels, handlers) under `config/logging/` and logging
   *wiring* (setup call, middleware, accessor) under `backend/app/core/`. The
   prior build consolidated everything in `core/logging.py`; `config/logging/`
   holds only a `.gitkeep`. Relocating would mean rewriting working, tested
   code with no functional gain. Decision: leave as-is, record the deviation
   here so future phases and reviewers know it is intentional.

2. **`CreatedAtMixin` added standalone, `TimestampMixin` untouched** — `audit_log`
   needs `created_at` only (NFR-R4: append-only). The existing `TimestampMixin`
   carries both `created_at` and `updated_at` and is already tested (P2-T1
   Validate gate). A new single-responsibility `CreatedAtMixin` was added rather
   than modifying `TimestampMixin` or skipping a mixin altogether, preserving the
   one-convention-per-mixin discipline and leaving the P2-T1 test surface intact.

3. **`record_audit` flushes but does not commit** — the audit row participates in
   the caller's unit of work. A separate commit would allow a window where the
   business mutation committed but the audit row had not (or vice versa). Flush
   ensures server-side `id` and `created_at` are populated and DB constraints
   surface before returning.

4. **500 path: safe message to client, full traceback to logs** — `unhandled_
   exception_handler` (pre-existing) logs `exc_info=exc` with the correlation ID
   and returns a generic `"An unexpected error occurred."` message. Stack traces
   and internal detail never reach the client (Dev Rule 9; security rule). The
   correlation ID is the client's handle to correlate with the server-side log.

5. **`AppError` carries only a safe client-facing message** — the `message` field
   on `AppError` is returned verbatim in the error envelope. It must not contain
   secrets, PII, or internal detail. Subclasses default to standard messages;
   callers may override with a user-safe description.

6. **Pagination is page/size with clamping, not rejection** — out-of-range
   `page`/`size` values are silently clamped (page ≤ 0 → 1; size > MAX →
   `MAX_PAGE_SIZE`) rather than returning a 400. Filter and sort parsing is the
   inverse: any unknown field raises `BadRequestError` immediately (deny-by-default
   — no silent passthrough to the query layer).

7. **FKs deferred to P3-T1** — `organization_id` (via `TenantMixin`) and
   `actor_id` on `audit_log` are plain UUID columns without `ForeignKey(...)`. The
   Phase 2/3 pairing note authorises building the model before the referenced
   tables exist; the FK constraints are the P3-T1 responsibility.

---

## Testing Considerations

- **Hermetic CI stays green** — `test_error_contract.py` and `test_pagination.py`
  need no Postgres. `test_audit.py` is marked `@pytest.mark.db` and self-skips
  (`pytest.skip(...)`) when the database is unreachable, exactly as the P2-T1
  `test_db_conventions.py` does.
- **Live Postgres gate** — with Postgres available, all 39 tests pass (4 live-DB
  tests run rather than skip). The audit Validate condition was verified against a
  real pg16 instance: exactly one `audit_log` row, correct column values,
  `created_at` set by the server, and no `updated_at`/`version`/`deleted_at`
  columns present.
- **Full CI-equivalent gate ran and passed**: `ruff check .` (all checks passed),
  `ruff format --check .` (31 files clean), `mypy backend config` (success: no
  issues in 24 source files), `pytest` (39 passed / 4 skipped hermetically).

---

## Risks Identified

- **Logging-placement deviation** — `core/logging.py` diverges from the
  `MASTER_EXECUTION_PLAN` Section 3 layout (`config/logging/` for config,
  `core/` for wiring). Recorded here for visibility. Future phases referencing
  "logging config location" should check `backend/app/core/logging.py`, not
  `config/logging/`.
- **`audit_log` FK gap** — until P3-T1 lands the `organizations` and `users`
  tables, `audit_log.organization_id` and `audit_log.actor_id` have no FK
  constraint. Invalid UUIDs can be inserted without a DB-level rejection. The
  application layer must validate these values until P3-T1 wires the constraints.

---

## Resolution Actions

- Duplicate `X-Correlation-ID` response header (pre-existing latent defect in the
  delivered correlation middleware): the middleware's `send_with_header` wrapper
  was `append`-ing a second copy on top of the one that `_envelope()` already
  embedded in the `JSONResponse`. Fixed by changing `.append()` to direct header
  assignment (`headers[CORRELATION_ID_HEADER] = cid`), ensuring exactly one header
  on all response paths.
- Test mypy errors: the test file's `add_exception_handler()` registrations
  triggered the same known Starlette-stub `arg-type` false-positive that
  `main_entry/app.py` handles via `tool.mypy.overrides`. Scoped
  `# type: ignore[arg-type]` on those three lines in the test, matching the
  existing project pattern. `Page[int]` annotation added to two `Page.create()`
  calls for generic inference.
