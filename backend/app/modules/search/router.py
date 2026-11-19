"""Global search router (product gap 3).

  GET /api/v1/search?q=<2..100 chars>&limit=<1..10 per group, default 5>

Any authenticated user; every group is scoped to what the caller may read (see
service.py), and groups the caller has no read permission for are omitted.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import get_current_user
from backend.app.modules.search import service
from backend.app.modules.search.schemas import SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
def search(
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int | None, Query()] = None,
) -> SearchResponse:
    return service.search(db, current_user, raw_query=q, limit=limit)
