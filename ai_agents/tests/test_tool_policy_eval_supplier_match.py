"""Hermetic tests for preferred_supplier matching (AIR-PREF / HOTEL-PREF).

No database, no LLM, no network. Covers:
  - normalize_supplier_name / supplier_matches_preferred: case- and
    punctuation-insensitive, generic-suffix-stripping, token-prefix (never
    substring) matching.
  - _eval_preferred_supplier: OCR-style vendor spellings pass AIR-PREF;
    non-preferred carriers still fail as blocking with the manager approver;
    a missing supplier stays a blocking AIR-PREF failure (pinned by the M-A3
    gold label m_a3_flight_empty_supplier_violation) and a silent in-policy
    HOTEL-PREF advisory (no new not_evaluated flag).
  - build_eval_item_from_fields: airfare/lodging carry the receipt vendor as
    supplier; other categories do not.
  - End-to-end hermetic chain for an uploaded airfare receipt.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item_from_fields,
    build_flags_snapshot,
    verdicts_to_flags,
)
from ai_agents.context import AgentContext
from ai_agents.tools.policy_eval import (
    EvalItem,
    PolicyEvalTool,
    normalize_supplier_name,
    supplier_matches_preferred,
)
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)

_AIR_PREFERRED = ["United Airlines", "Delta Air Lines", "Lufthansa"]
_HOTEL_PREFERRED = ["Marriott International", "Hilton"]


def _air_pref_rule() -> PolicyRule:
    """In-memory AIR-PREF rule mirroring the seed definition."""
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.preferred_supplier,
        applies_to=PolicyAppliesTo.flight,
        source_clause_ref="AIR-PREF",
        status=PolicyRuleStatus.active,
        definition={
            "preferred_suppliers": list(_AIR_PREFERRED),
            "within_pct": 10,
            "exception_approver": "manager",
        },
    )


def _hotel_pref_rule() -> PolicyRule:
    """In-memory HOTEL-PREF (advisory) rule mirroring the seed definition."""
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.preferred_supplier,
        applies_to=PolicyAppliesTo.hotel,
        source_clause_ref="HOTEL-PREF",
        status=PolicyRuleStatus.active,
        definition={
            "preferred_suppliers": list(_HOTEL_PREFERRED),
            "condition": "when available at capped rate",
        },
    )


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        user_id=uuid.uuid4(),
        correlation_id="supplier-match-test",
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeSupplierName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("United Airlines", ("united",)),
            ("UNITED", ("united",)),
            ("United Airlines Inc.", ("united",)),
            ("Delta Air Lines", ("delta",)),
            ("DELTA AIR LINES, INC.", ("delta",)),
            ("Lufthansa German Airlines", ("lufthansa", "german")),
            ("Marriott International", ("marriott",)),
            ("Hilton Hotels & Resorts", ("hilton",)),
            ("Deutsche Lufthansa AG", ("lufthansa",)),
            ("Air Canada", ("air", "canada")),  # brand-bearing "air" is kept
            ("Airlines", ("airlines",)),  # never collapses to an empty key
            ("", ()),
            (None, ()),
        ],
    )
    def test_normalize(self, raw: str | None, expected: tuple[str, ...]) -> None:
        assert normalize_supplier_name(raw) == expected


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestSupplierMatchesPreferred:
    @pytest.mark.parametrize(
        "supplier",
        [
            "United Airlines",
            "UNITED",
            "United Airlines Inc.",
            "united airlines",
            "Delta Air Lines",
            "DELTA",
            "Delta Air Lines, Inc.",
            "Lufthansa",
            "LUFTHANSA",
            "Deutsche Lufthansa AG",
        ],
    )
    def test_air_preferred(self, supplier: str) -> None:
        assert supplier_matches_preferred(supplier, _AIR_PREFERRED) is True

    @pytest.mark.parametrize(
        "supplier",
        [
            "American Airlines",
            "Spirit",
            "Spirit Airlines",
            "UnitedHealth",  # token-based: no substring match on "united"
            "JetBlue",
            "Air Canada",
            "Airlines",
            "",
            # Same first word, different carrier: a blocking rule needs an exact
            # normalized brand match, never a leading-token prefix.
            "United Nigeria Airlines",
            "Delta Connection Partners",
            "Lufthansa German Airlines",  # conservative: needs approval, not a silent pass
        ],
    )
    def test_air_not_preferred(self, supplier: str) -> None:
        assert supplier_matches_preferred(supplier, _AIR_PREFERRED) is False

    def test_preferred_key_must_be_leading_prefix(self) -> None:
        # "delta" appears, but not as the leading token → no match.
        assert supplier_matches_preferred("Air Delta Express", _AIR_PREFERRED) is False

    @pytest.mark.parametrize(
        "supplier", ["Marriott New York", "MARRIOTT", "Marriott International", "Hilton Garden Inn"]
    )
    def test_hotel_preferred_with_prefix(self, supplier: str) -> None:
        # Advisory (HOTEL-PREF) evaluation opts into property-name prefixes.
        assert supplier_matches_preferred(supplier, _HOTEL_PREFERRED, allow_prefix=True) is True

    def test_hotel_not_preferred(self) -> None:
        assert (
            supplier_matches_preferred("Independent", _HOTEL_PREFERRED, allow_prefix=True) is False
        )

    def test_prefix_not_allowed_by_default(self) -> None:
        assert supplier_matches_preferred("Marriott New York", _HOTEL_PREFERRED) is False
        assert supplier_matches_preferred("MARRIOTT", _HOTEL_PREFERRED) is True

    def test_exact_name_still_matches(self) -> None:
        # Planner / inventory pass canonical names verbatim — unchanged behavior.
        for name in _AIR_PREFERRED:
            assert supplier_matches_preferred(name, _AIR_PREFERRED) is True


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


class TestEvalPreferredSupplier:
    @pytest.mark.parametrize(
        "supplier",
        [
            "United Airlines",
            "UNITED",
            "United Airlines Inc.",
            "Delta Air Lines",
            "DELTA",
            "Lufthansa",
            "Deutsche Lufthansa AG",
        ],
    )
    def test_preferred_carrier_in_policy(self, supplier: str) -> None:
        v = PolicyEvalTool()._evaluate_rule(
            _air_pref_rule(), EvalItem(item_type="flight", supplier=supplier)
        )
        assert v.in_policy is True
        assert v.requires_approver is None
        assert v.observed["supplier"] == supplier

    @pytest.mark.parametrize("supplier", ["American Airlines", "Spirit", "UnitedHealth"])
    def test_non_preferred_carrier_blocks(self, supplier: str) -> None:
        v = PolicyEvalTool()._evaluate_rule(
            _air_pref_rule(), EvalItem(item_type="flight", supplier=supplier)
        )
        assert v.in_policy is False
        assert v.requires_approver == "manager"
        assert v.source_clause_ref == "AIR-PREF"

    def test_hotel_marriott_new_york_preferred(self) -> None:
        v = PolicyEvalTool()._evaluate_rule(
            _hotel_pref_rule(), EvalItem(item_type="hotel", supplier="Marriott New York")
        )
        assert v.in_policy is True
        assert "is in the preferred list" in v.reason

    def test_hotel_non_preferred_is_advisory(self) -> None:
        v = PolicyEvalTool()._evaluate_rule(
            _hotel_pref_rule(), EvalItem(item_type="hotel", supplier="Independent")
        )
        assert v.in_policy is True
        assert "advised but not required" in v.reason

    @pytest.mark.parametrize("supplier", [None, "", "   "])
    def test_missing_supplier_air_stays_blocking(self, supplier: str | None) -> None:
        # Gold-label contract (M-A3 m_a3_flight_empty_supplier_violation):
        # an unknown carrier is a violation, not a skip.
        v = PolicyEvalTool()._evaluate_rule(
            _air_pref_rule(), EvalItem(item_type="flight", supplier=supplier)
        )
        assert v.in_policy is False
        assert v.requires_approver == "manager"
        assert v.observed["supplier"] == ""

        flags, failing = verdicts_to_flags(
            [v], entry_ref="r1", has_receipt=True, category="airfare"
        )
        assert failing == 1
        assert [f.severity for f in flags] == ["blocking"]

    @pytest.mark.parametrize("supplier", [None, ""])
    def test_missing_supplier_hotel_produces_no_flag(self, supplier: str | None) -> None:
        # HOTEL-PREF is advisory: an unknown hotel brand is in-policy with
        # non-empty observed → no flag (no new not_evaluated advisory).
        v = PolicyEvalTool()._evaluate_rule(
            _hotel_pref_rule(), EvalItem(item_type="hotel", supplier=supplier)
        )
        assert v.in_policy is True
        assert v.observed != {}
        flags, failing = verdicts_to_flags(
            [v], entry_ref="h1", has_receipt=True, category="lodging"
        )
        assert failing == 0
        assert flags == []


# ---------------------------------------------------------------------------
# build_eval_item_from_fields — supplier plumbing
# ---------------------------------------------------------------------------


class TestBuildEvalItemSupplier:
    def test_airfare_sets_supplier_from_vendor(self) -> None:
        item = build_eval_item_from_fields(
            {"amount": 420.0, "vendor": "United Airlines"}, category="airfare", ref_id="r"
        )
        assert item.item_type == "flight"
        assert item.supplier == "United Airlines"

    def test_lodging_sets_supplier_from_vendor(self) -> None:
        item = build_eval_item_from_fields(
            {"amount": 210.0, "vendor": "Marriott New York", "location": "New York, NY"},
            category="lodging",
            ref_id="r",
        )
        assert item.item_type == "hotel"
        assert item.supplier == "Marriott New York"

    @pytest.mark.parametrize("category", ["meals", "ground_transport", "other"])
    def test_other_categories_leave_supplier_unset(self, category: str) -> None:
        item = build_eval_item_from_fields(
            {"amount": 20.0, "vendor": "Some Vendor"}, category=category, ref_id="r"
        )
        assert item.supplier is None

    @pytest.mark.parametrize("vendor", [None, "", "  ", 123])
    def test_blank_or_non_string_vendor_is_none(self, vendor: object) -> None:
        item = build_eval_item_from_fields(
            {"amount": 420.0, "vendor": vendor}, category="airfare", ref_id="r"
        )
        assert item.supplier is None


# ---------------------------------------------------------------------------
# Hermetic chain: uploaded airfare receipt → verdict → flags
# ---------------------------------------------------------------------------


def _run_chain(vendor: str) -> dict:
    item = build_eval_item_from_fields(
        {"amount": 420.0, "vendor": vendor, "expense_type": "airfare"},
        category="airfare",
        ref_id=vendor,
    )
    tool = PolicyEvalTool()
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=[_air_pref_rule()]):
        verdict = tool.evaluate(item, context=_ctx(), session=None)  # type: ignore[arg-type]
    flags, _ = verdicts_to_flags(
        list(verdict.verdicts), entry_ref=vendor, has_receipt=True, category="airfare"
    )
    return {"verdict": verdict, "snapshot": build_flags_snapshot(flags, entry_count=1)}


class TestAirfareReceiptChain:
    @pytest.mark.parametrize("vendor", ["United Airlines", "UNITED", "Delta", "Lufthansa"])
    def test_preferred_vendor_not_blocking(self, vendor: str) -> None:
        out = _run_chain(vendor)
        assert out["verdict"].in_policy is True
        assert out["snapshot"]["blocking_count"] == 0

    def test_spirit_vendor_blocks(self) -> None:
        out = _run_chain("Spirit Airlines")
        assert out["verdict"].in_policy is False
        assert out["snapshot"]["blocking_count"] == 1
        flag = out["snapshot"]["flags"][0]
        assert flag["source_clause_ref"] == "AIR-PREF"
        assert flag["severity"] == "blocking"
        assert flag["requires_approver"] == "manager"


# ---------------------------------------------------------------------------
# Trust boundary: only a vendor read from the receipt may clear the rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        (None, "United Airlines"),
        ({"vendor": "deterministic"}, "United Airlines"),
        ({"vendor": "ai_inferred"}, None),
        ({"vendor": "user_edited"}, None),
        ({}, None),
    ],
)
def test_supplier_requires_deterministic_vendor(
    provenance: dict[str, str] | None, expected: str | None
) -> None:
    item = build_eval_item_from_fields(
        {"vendor": "United Airlines", "amount": 450.0},
        category="airfare",
        ref_id="r1",
        provenance=provenance,
    )
    assert item.supplier == expected
