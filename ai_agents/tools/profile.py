"""ProfileTool — grounded user profile and travel history (P4-T4).

Reads the context user's `users` row (preferences JSONB) and a lightweight
history of recent trips.  Org + user scoped.  NULL-safe: preferences=None
in the DB → empty dict in the result.

Raises ToolError if context.user_id is None (system principal has no profile).

Downstream seam: profile data feeds the Travel Agent (P4-T9) for personalization.
Fields → Citation(kind=source_field, ref="user.preferences", source="users").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from database.models.identity import User
from database.models.trip import Trip

logger = logging.getLogger(__name__)

_DEFAULT_RECENT_TRIPS_LIMIT = 5


@dataclass(frozen=True)
class ProfileIntent:
    """Options for the profile lookup."""

    recent_trips_limit: int = _DEFAULT_RECENT_TRIPS_LIMIT


@dataclass(frozen=True)
class TripSummary:
    """Lightweight trip summary for profile history and itinerary tools."""

    trip_id: str
    origin: str
    primary_destination: str
    start_date: str  # ISO date string
    end_date: str  # ISO date string
    status: str
    estimated_cost: float | None
    budget_amount: float | None
    currency: str


@dataclass(frozen=True)
class ProfileResult:
    """User profile + recent trip history.

    preferences is always a dict (never None) — null-safe.
    Fields → Citation(kind=source_field, ref="user.preferences", source=source).
    """

    user_id: str
    email: str
    role: str
    department: str | None
    manager_id: str | None
    preferences: dict  # NULL-safe: DB NULL → {}
    recent_trips: tuple[TripSummary, ...]
    source: str = "users"  # → Citation.source


def _date_to_str(d: date | None) -> str:
    return d.isoformat() if d is not None else ""


class ProfileTool:
    """Grounding tool: user profile and recent trip history (read-only, org+user scoped)."""

    def get_profile(
        self,
        intent: ProfileIntent,
        *,
        context: AgentContext,
        session: Session,
    ) -> ProfileResult:
        """Return the context user's profile and recent trips.

        Raises ToolError if context.user_id is None.
        """
        if context.user_id is None:
            raise ToolError(
                "ProfileTool requires a user principal (context.user_id is None).",
                code="PROFILE_TOOL",
            )

        user = session.scalar(
            sa.select(User).where(
                User.id == context.user_id,
                User.organization_id == context.organization_id,
            )
        )
        if user is None:
            raise ToolError(
                "User not found for the given context (user_id / org_id mismatch).",
                code="PROFILE_TOOL",
            )

        trips = session.scalars(
            sa.select(Trip)
            .where(
                Trip.traveler_id == context.user_id,
                Trip.organization_id == context.organization_id,
            )
            .order_by(Trip.start_date.desc())
            .limit(intent.recent_trips_limit)
        ).all()

        return ProfileResult(
            user_id=str(user.id),
            email=user.email,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            department=user.department,
            manager_id=str(user.manager_id) if user.manager_id is not None else None,
            preferences=user.preferences or {},
            recent_trips=tuple(
                TripSummary(
                    trip_id=str(t.id),
                    origin=t.origin,
                    primary_destination=t.primary_destination,
                    start_date=_date_to_str(t.start_date),  # type: ignore[arg-type]
                    end_date=_date_to_str(t.end_date),  # type: ignore[arg-type]
                    status=t.status.value if hasattr(t.status, "value") else str(t.status),
                    estimated_cost=(
                        float(t.estimated_cost)  # type: ignore[arg-type]
                        if t.estimated_cost is not None
                        else None
                    ),
                    budget_amount=(
                        float(t.budget_amount)  # type: ignore[arg-type]
                        if t.budget_amount is not None
                        else None
                    ),
                    currency=t.currency,
                )
                for t in trips
            ),
        )
