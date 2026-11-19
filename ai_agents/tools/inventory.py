"""InventoryTool — hermetic grounding tool for mock travel inventory (P4-T4).

Wraps MockInventoryProvider (P4-T1) and maps Flight/Hotel dataclasses to
typed InventoryOption results.  Entirely hermetic — no DB, no tenant filter
(the dataset is global mock data).  AgentContext is accepted for signature
uniformity but not used for filtering.

Downstream seam: results feed the Travel Agent (P4-T9) and the PolicyEval
tool (option → EvalItem).  option_id → Citation(kind=tool_result, ref=option_id,
source="mock_inventory").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from services.mock_inventory.provider import MockInventoryProvider, get_inventory_provider


@dataclass(frozen=True)
class InventoryIntent:
    """Structured search intent for the inventory tool."""

    segment_type: Literal["flight", "hotel"]
    # Flight fields (required when segment_type="flight")
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None  # ISO date "YYYY-MM-DD"
    # Hotel fields (required when segment_type="hotel")
    city: str | None = None
    check_in_date: str | None = None  # ISO date "YYYY-MM-DD"
    check_out_date: str | None = None  # ISO date "YYYY-MM-DD"


@dataclass(frozen=True)
class InventoryOption:
    """One search result from the mock inventory.

    option_id → Citation(kind=tool_result, ref=option_id, source="mock_inventory").
    """

    option_id: str  # Flight.flight_id / Hotel.hotel_id
    segment_type: str  # "flight" | "hotel"
    supplier: str  # airline / hotel brand — feeds preferred_supplier eval
    amount_usd: float  # price_usd / nightly_rate_usd
    sustainability_score: int  # 1 (low-emission) – 5 (high-emission)
    # Flight-only (None for hotels)
    cabin_class: str | None
    duration_hours: float | None
    is_direct: bool | None
    # Hotel-only (None for flights)
    city: str | None
    nightly_rate_usd: float | None
    room_type: str | None
    # Full source dataclass as dict — passthrough for PolicyEval.evaluate()
    raw: dict


@dataclass(frozen=True)
class InventoryResult:
    """Results of an inventory search."""

    intent: InventoryIntent
    options: tuple[InventoryOption, ...]
    source: str = "mock_inventory"  # → Citation.source


class InventoryTool:
    """Grounding tool: structured travel inventory search (hermetic).

    Constructor: inject provider for tests; defaults to the factory singleton.
    Per-call: no session needed (data is entirely in-memory).
    """

    def __init__(self, provider: MockInventoryProvider | None = None) -> None:
        self._provider = provider or get_inventory_provider()

    def search(self, intent: InventoryIntent, *, context: AgentContext) -> InventoryResult:
        """Return available options matching the intent.

        Empty results (no match) → empty tuple, not an error.
        Unknown segment_type or missing required fields → ToolError.
        """
        if intent.segment_type == "flight":
            return self._search_flights(intent, context=context)
        if intent.segment_type == "hotel":
            return self._search_hotels(intent, context=context)
        raise ToolError(
            f"Unknown segment_type: {intent.segment_type!r}. Must be 'flight' or 'hotel'.",
            code="INVENTORY_TOOL",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_flights(self, intent: InventoryIntent, *, context: AgentContext) -> InventoryResult:
        for field in ("origin", "destination", "departure_date"):
            if getattr(intent, field) is None:
                raise ToolError(
                    f"Flight search requires '{field}' but got None.",
                    code="INVENTORY_TOOL",
                )
        flights = self._provider.search_flights(
            origin=intent.origin,  # type: ignore[arg-type]
            destination=intent.destination,  # type: ignore[arg-type]
            departure_date=intent.departure_date,  # type: ignore[arg-type]
        )
        options = tuple(
            InventoryOption(
                option_id=f.flight_id,
                segment_type="flight",
                supplier=f.airline,
                amount_usd=f.price_usd,
                sustainability_score=f.sustainability_score,
                cabin_class=f.cabin_class,
                duration_hours=f.duration_hours,
                is_direct=f.is_direct,
                city=None,
                nightly_rate_usd=None,
                room_type=None,
                raw=asdict(f),
            )
            for f in flights
        )
        return InventoryResult(intent=intent, options=options)

    def _search_hotels(self, intent: InventoryIntent, *, context: AgentContext) -> InventoryResult:
        for field in ("city", "check_in_date", "check_out_date"):
            if getattr(intent, field) is None:
                raise ToolError(
                    f"Hotel search requires '{field}' but got None.",
                    code="INVENTORY_TOOL",
                )
        hotels = self._provider.search_hotels(
            city=intent.city,  # type: ignore[arg-type]
            check_in_date=intent.check_in_date,  # type: ignore[arg-type]
            check_out_date=intent.check_out_date,  # type: ignore[arg-type]
        )
        options = tuple(
            InventoryOption(
                option_id=h.hotel_id,
                segment_type="hotel",
                supplier=h.brand,
                amount_usd=h.nightly_rate_usd,
                sustainability_score=h.sustainability_score,
                cabin_class=None,
                duration_hours=None,
                is_direct=None,
                city=h.city,
                nightly_rate_usd=h.nightly_rate_usd,
                room_type=h.room_type,
                raw=asdict(h),
            )
            for h in hotels
        )
        return InventoryResult(intent=intent, options=options)
