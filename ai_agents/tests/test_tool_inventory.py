"""Hermetic tests for InventoryTool (P4-T4).

No database, no markers — runs in hermetic CI.
Proves the inventory tool wraps the seeded mock dataset correctly and
that option_ids are citation-able.
"""

from __future__ import annotations

import uuid

import pytest

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from ai_agents.tools.inventory import (
    InventoryIntent,
    InventoryResult,
    InventoryTool,
)


def _ctx(org_id: uuid.UUID | None = None) -> AgentContext:
    return AgentContext(
        organization_id=org_id or uuid.uuid4(),
        granted_permission_keys=frozenset(),
        user_id=uuid.uuid4(),
        correlation_id="inv-test",
    )


@pytest.fixture()
def tool() -> InventoryTool:
    return InventoryTool()  # uses the default factory (hermetic mock dataset)


# ---------------------------------------------------------------------------
# Flight search
# ---------------------------------------------------------------------------


class TestFlightSearch:
    def test_sfo_jfk_returns_multiple_options(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        assert isinstance(result, InventoryResult)
        assert len(result.options) >= 3, "Expected ≥3 SFO→JFK options from seed dataset"

    def test_out_of_policy_flight_present(self, tool: InventoryTool) -> None:
        """UA201B business short-haul must be in the dataset for policy-eval tests."""
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        ids = {o.option_id for o in result.options}
        assert "UA201B-2026-06-10" in ids

    def test_out_of_policy_flight_has_business_cabin(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        oop_option = next(o for o in result.options if o.option_id == "UA201B-2026-06-10")
        assert oop_option.cabin_class == "business"
        assert oop_option.duration_hours == 5.5

    def test_option_ids_populated_for_citation(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        for opt in result.options:
            assert opt.option_id, f"option_id must be non-empty, got {opt!r}"

    def test_impossible_route_returns_empty_not_fabricated(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="ZZZ",
            destination="QQQ",
            departure_date="2099-12-31",
        )
        result = tool.search(intent, context=_ctx())
        assert result.options == ()

    def test_source_field(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        assert result.source == "mock_inventory"

    def test_raw_dict_present_for_policy_eval_passthrough(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
        )
        result = tool.search(intent, context=_ctx())
        for opt in result.options:
            assert isinstance(opt.raw, dict)
            assert "cabin_class" in opt.raw


# ---------------------------------------------------------------------------
# Hotel search
# ---------------------------------------------------------------------------


class TestHotelSearch:
    def test_nyc_hotels_include_out_of_policy(self, tool: InventoryTool) -> None:
        """The Luxury Loft NYC ($400/night) must be in the dataset for policy-eval tests."""
        intent = InventoryIntent(
            segment_type="hotel",
            city="New York",
            check_in_date="2026-06-10",
            check_out_date="2026-06-14",
        )
        result = tool.search(intent, context=_ctx())
        ids = {o.option_id for o in result.options}
        assert "NYC-BOUTIQUE-001" in ids

    def test_oop_hotel_has_correct_nightly_rate(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="hotel",
            city="New York",
            check_in_date="2026-06-10",
            check_out_date="2026-06-14",
        )
        result = tool.search(intent, context=_ctx())
        oop = next(o for o in result.options if o.option_id == "NYC-BOUTIQUE-001")
        assert oop.nightly_rate_usd == 400.00

    def test_unknown_city_returns_empty(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="hotel",
            city="Atlantis",
            check_in_date="2026-06-10",
            check_out_date="2026-06-14",
        )
        result = tool.search(intent, context=_ctx())
        assert result.options == ()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_bad_segment_type_raises_tool_error(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(segment_type="train")  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="Unknown segment_type"):
            tool.search(intent, context=_ctx())

    def test_flight_missing_origin_raises_tool_error(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="flight",
            destination="JFK",
            departure_date="2026-06-10",
        )
        with pytest.raises(ToolError, match="origin"):
            tool.search(intent, context=_ctx())

    def test_hotel_missing_city_raises_tool_error(self, tool: InventoryTool) -> None:
        intent = InventoryIntent(
            segment_type="hotel",
            check_in_date="2026-06-10",
            check_out_date="2026-06-14",
        )
        with pytest.raises(ToolError, match="city"):
            tool.search(intent, context=_ctx())

    def test_tool_error_is_not_backend_error(self, tool: InventoryTool) -> None:
        """ToolError must not be an AppError (layering law)."""
        intent = InventoryIntent(segment_type="bad")  # type: ignore[arg-type]
        with pytest.raises(ToolError) as exc_info:
            tool.search(intent, context=_ctx())
        # Must not be from backend — GuardrailError base, no backend module
        cls = type(exc_info.value)
        assert "backend" not in cls.__module__
