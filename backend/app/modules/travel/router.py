"""Travel module router — trip + segment endpoints.

Mounted at /api/v1/trips (prefix added in backend/app/api/v1.py).
  POST   /api/v1/trips                   create trip
  GET    /api/v1/trips                   list trips (scoped + paginated)
  GET    /api/v1/trips/{trip_id}         get trip detail
  PATCH  /api/v1/trips/{trip_id}         state transition
  GET    /api/v1/trips/{trip_id}/segments  trip segments

Thin router — all business logic lives in service.py (Dev Rule 5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import require_permission
from backend.app.modules.travel import schemas, service
from config.constants.permissions import TRIP_CREATE_OWN, TRIP_READ_OWN, TRIP_UPDATE_OWN
from database.models.identity import User

router = APIRouter(tags=["trips"])

# Query-string keys consumed by pagination/sort/scope; excluded from filter parsing.
_RESERVED_KEYS = {"page", "size", "sort", "mine"}


@router.post("", response_model=schemas.TripResponse, status_code=201)
def create_trip(
    body: schemas.TripCreate,
    current_user: Annotated[User, Depends(require_permission(TRIP_CREATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.TripResponse:
    """Create a new trip in draft status (requires trip.create.own permission)."""
    return service.create_trip(db, current_user, body)


@router.get("", response_model=Page[schemas.TripListItem])
def list_trips(
    request: Request,
    current_user: Annotated[User, Depends(require_permission(TRIP_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(
        None, description="Sort spec, e.g. 'start_date:asc' or '-created_at'."
    ),
    mine: bool = Query(
        False,
        description=(
            "true → only trips the caller travels on (the personal 'My Trips' view). "
            "Default: the caller's full role read scope."
        ),
    ),
) -> Page[schemas.TripListItem]:
    """List trips scoped by role (requires trip.read.own permission).

    employee→own, manager→own+reports, finance_admin→org-wide.  ``?mine=true``
    narrows any role to its own trips (UAT2 — personal pages always send it).
    Supports ?page, ?size, ?sort, and allow-listed filter fields.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _RESERVED_KEYS}
    return service.list_trips(db, current_user, pagination, raw_filters, sort, mine=mine)


@router.get("/{trip_id}", response_model=schemas.TripResponse)
def get_trip(
    trip_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(TRIP_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.TripResponse:
    """Fetch a single trip by ID (requires trip.read.own permission).

    Returns 404 for both out-of-scope and nonexistent IDs.
    """
    return service.get_trip(db, current_user, trip_id)


@router.patch("/{trip_id}", response_model=schemas.TripResponse)
def transition_trip(
    trip_id: uuid.UUID,
    body: schemas.TripTransition,
    current_user: Annotated[User, Depends(require_permission(TRIP_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.TripResponse:
    """Transition a trip's status (requires trip.update.own permission).

    Legal transitions per LEGAL_TRANSITIONS map.  Illegal → 409 Conflict.
    """
    return service.transition_trip(db, current_user, trip_id, body.status)


@router.get("/{trip_id}/segments", response_model=list[schemas.SegmentResponse])
def get_trip_segments(
    trip_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(TRIP_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> list[schemas.SegmentResponse]:
    """Return segments for a trip (requires trip.read.own permission).

    Returns 404 if the parent trip is out of scope or nonexistent.
    """
    return service.get_trip_segments(db, current_user, trip_id)
