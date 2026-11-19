"""Analytics module router — role-scoped dashboard overview.

Mounted at /api/v1/analytics (prefix added in backend/app/api/v1.py).
  GET /api/v1/analytics/overview  — returns role-scoped dashboard data

Thin router — all business logic lives in service.py (Dev Rule 5).
Response is inherently role-scoped server-side; no additional permission gate
beyond authentication is required.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import get_current_user
from backend.app.modules.analytics.schemas import AnalyticsOverviewResponse
from backend.app.modules.analytics.service import get_overview
from database.models.identity import User

router = APIRouter(tags=["analytics"])


@router.get("/overview", response_model=AnalyticsOverviewResponse)
def get_analytics_overview(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> AnalyticsOverviewResponse:
    """Return a role-scoped analytics overview for the authenticated user.

    Response shape depends on the user's role:
      - employee    → employee field populated (kpis, recommendations, policy warnings)
      - manager     → manager field populated (approval queue, team spend, activity)
      - finance_admin → finance field populated (kpis, spend breakdowns, activity)
    """
    return get_overview(session=session, current_user=current_user)
