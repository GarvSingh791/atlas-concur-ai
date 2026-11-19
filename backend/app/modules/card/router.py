"""Card module router (product gap 8) — the caller's corporate-card feed.

  GET /api/v1/card-transactions  — the caller's OWN card swipes (paginated)

Mounted with no prefix (paths are full literals, like the receipt router).
Gated by ``expense.create.own`` — the same gate as the Receipts area it lives
in (employee + manager; finance_admin has no receipts access and gets 403).
Every role sees only its own transactions (``owner_scope_query``).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import require_permission
from backend.app.modules.card import service
from backend.app.modules.card.schemas import CardTransactionItem, CardTransactionStatus
from config.constants.permissions import EXPENSE_CREATE_OWN
from database.models.identity import User

router = APIRouter(tags=["cards"])


@router.get("/card-transactions", response_model=Page[CardTransactionItem])
def list_card_transactions(
    user: Annotated[User, Depends(require_permission(EXPENSE_CREATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    status: Annotated[
        CardTransactionStatus | None,
        Query(description="'matched' (backs an expense entry) or 'unmatched'."),
    ] = None,
    date_from: Annotated[
        date | None, Query(description="Earliest transaction date (inclusive).")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Latest transaction date (inclusive).")
    ] = None,
) -> Page[CardTransactionItem]:
    """List the caller's own card transactions, newest first."""
    return service.list_card_transactions(
        db, user, pagination, status=status, date_from=date_from, date_to=date_to
    )
