"""
/api/v1 aggregate router.

All versioned API routes are registered here.  Each domain module exposes a
single FastAPI router; one include_router() call below wires it in.

HOW TO ADD A MODULE ROUTER (example — uncomment when the module is ready):
    from backend.app.modules.identity.router import router as identity_router
    api_router.include_router(identity_router, prefix="/identity", tags=["identity"])

Planned modules (in build order per MASTER_EXECUTION_PLAN §5):
    /identity   — auth, users, RBAC            (Phase 2 — P2-T3..T5)
    /trips      — travel planning + segments   (Phase 2 — P2-T6)
    /expenses   — reports + entries            (Phase 2 — P2-T7)
    /receipts   — image intake + extraction    (Phase 2 — P2-T6 ext.)
    /policy     — rules + RAG Q&A              (Phase 4 — P4-T8)
    /approvals  — workflow + decisions         (Phase 4 — P4-T12)
    /analytics  — role-scoped aggregations     (Phase 5 — P5-T4)
    /ai         — conversations + agents       (Phase 4 — P4-T13)
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.ops import router as ops_router
from backend.app.api.public_config import router as public_config_router
from backend.app.core.config import get_settings
from backend.app.modules.ai.router import router as ai_router
from backend.app.modules.analytics.router import router as analytics_router
from backend.app.modules.approval.router import router as approval_router
from backend.app.modules.audit.router import router as audit_router
from backend.app.modules.card.router import router as card_router
from backend.app.modules.expense.router import router as expense_router
from backend.app.modules.identity.router import router as identity_router
from backend.app.modules.lifecycle.router import router as lifecycle_router
from backend.app.modules.notifications.router import router as notifications_router
from backend.app.modules.policy.router import router as policy_router
from backend.app.modules.receipt.router import router as receipt_router
from backend.app.modules.search.router import router as search_router
from backend.app.modules.team.router import router as team_router
from backend.app.modules.travel.router import router as travel_router

_settings = get_settings()

api_router = APIRouter(prefix=_settings.API_V1_PREFIX)

# ---------------------------------------------------------------------------
# Register module routers below — one include_router() per module.
# ---------------------------------------------------------------------------

# No prefix — auth paths match the design docs literally:
#   POST /api/v1/auth/login, POST /api/v1/auth/refresh, GET /api/v1/me
api_router.include_router(identity_router)

# Travel: POST/GET /api/v1/trips, GET/PATCH /api/v1/trips/{id},
#         GET /api/v1/trips/{id}/segments
api_router.include_router(travel_router, prefix="/trips", tags=["trips"])

# Expenses: POST/GET /api/v1/expense-reports, GET/PATCH /api/v1/expense-reports/{id},
#           GET /api/v1/expense-reports/{id}/entries, PATCH /api/v1/expenses/{id}
# Mounted with no prefix — paths are declared as full literals in the router.
api_router.include_router(expense_router)

# AI: POST /api/v1/conversations, POST /api/v1/conversations/{id}/messages,
#     GET /api/v1/conversations/{id}
api_router.include_router(ai_router, prefix="/conversations", tags=["ai"])

# Policy: POST /api/v1/policy/query, POST /api/v1/policy/evaluate,
#         POST /api/v1/policies/{id}/rules
api_router.include_router(policy_router, tags=["policy"])

# Receipts: POST /api/v1/receipts, PATCH /api/v1/receipts/{receipt_id}
# Mounted with no prefix — paths are declared as full literals in the router.
api_router.include_router(receipt_router)

# Approvals: POST /api/v1/approvals, PATCH /api/v1/approvals/{approval_id}
# Mounted with no prefix — paths are declared as full literals in the router.
api_router.include_router(approval_router)

# Analytics: GET /api/v1/analytics/overview — role-scoped dashboard aggregations
api_router.include_router(analytics_router, prefix="/analytics", tags=["analytics"])

# Audit: GET /api/v1/audit — paginated org-wide audit-log read (finance_admin)
api_router.include_router(audit_router, prefix="/audit", tags=["audit"])

# Lifecycle (product gaps 1/4/8): POST /api/v1/expense-reports/{id}/reimburse|withdraw,
#   POST /api/v1/trips/{id}/withdraw|complete, GET /api/v1/reimbursements/queue
api_router.include_router(lifecycle_router)

# Team (Product gap 5): GET /api/v1/team/members[/{user_id}] — manager / finance
api_router.include_router(team_router, prefix="/team", tags=["team"])

# Cards: GET /api/v1/card-transactions — the caller's own card feed (product gap 8)
api_router.include_router(card_router)

# ---------------------------------------------------------------------------


# Product gap 3 (app shell): GET /api/v1/notifications (+ /unread-count,
# /read-all, /{id}/read) and GET /api/v1/search — paths declared in the routers.
api_router.include_router(notifications_router)
api_router.include_router(search_router)

# Ops (phase 11): GET /api/v1/ops/rate-limits — read-only limiter status (finance_admin)
# (+ phase 12: GET /api/v1/ops/demo-reset — scheduled demo reset status, finance_admin)
api_router.include_router(ops_router)

# Phase 11: GET /api/v1/public/config — unauthenticated, non-secret client flags
# (the DEMO_MODE banner).
api_router.include_router(public_config_router)


# ---- Debug-only route (APP_DEBUG=true) -------------------------------------
# Lets the validate gate trigger a 500 without coupling test fixtures to real
# business logic.  Never exists in production (APP_DEBUG defaults to False).

if _settings.APP_DEBUG:
    _debug_router = APIRouter(prefix="/_debug", tags=["debug"])

    @_debug_router.get("/error", include_in_schema=False)
    async def _trigger_error() -> None:  # pragma: no cover
        raise RuntimeError("Intentional debug error — validate gate only")

    api_router.include_router(_debug_router)
