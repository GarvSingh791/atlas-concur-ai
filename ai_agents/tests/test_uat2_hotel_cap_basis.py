"""UAT2 Issue I — the hotel per-night figure honours the HOTEL-CAP rule's ``basis``.

Browser evidence: a 3-night Hyatt folio (Room total 690.00, Occupancy tax 60.00,
TOTAL 750.00) was evaluated as "Nightly rate 230.00 USD (3 nights)" — the OCR
room rate (690 / 3) — although the canonical HOTEL-CAP definition says
``basis: room_plus_tax`` (LOD-01: the nightly cap covers room + tax), so the
nightly figure is 750 / 3 = 250.00.

Rules proved here (hermetic: no DB, no LLM, no network):
  * room_plus_tax (canonical) — and an ABSENT or unknown basis, fail closed —
    compares (line total incl. tax) / nights; the OCR room-only rate never
    lowers it;
  * room_only (legacy) compares the room subtotal / nights, only where a
    trusted, consistent subtotal is known (0 < subtotal <= line amount);
    otherwise it falls back to the room+tax figure;
  * without verified nights, the OCR per-night rate only supplies an implied
    stay length when it divides the room subtotal (or the total) exactly; the
    nightly figure is then the TOTAL / implied nights.  Otherwise the full line
    amount is compared (fail closed);
  * the entry / approval-time path uses the same figures and only trusts an
    OCR subtotal while the claim still equals the OCR-read total.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from ai_agents.agents.expense_agent.presubmit import build_eval_item_from_fields
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    _assess,
    _ctx,
    _draft_entry,
    _entry,
    _presubmit,
    _pv,
    _receipt,
    _report,
    _seeded_rules,
    _session_with_users,
    _violations,
)
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import HOTEL_CAP_DEFINITION

DET = "deterministic"

# The Hyatt folio from the browser test (Q3 lodging line, 3 nights).
HYATT = {
    "vendor": "Hyatt Regency",
    "amount": 750.0,
    "subtotal": 690.0,
    "tax": 60.0,
    "nights": 3,
    "per_night_rate": 230.0,
    "location": "Austin, TX",
}
HYATT_PROV = {"vendor": DET, "amount": DET, "location": DET}


def _cap_rule(basis: str | None, cap: float = 240.0) -> PolicyRule:
    """A single-tier HOTEL-CAP rule: 230 (room only) passes, 250 (room + tax) fails."""
    definition: dict[str, Any] = {
        "currency": "USD",
        "per": "night",
        "tiers": [{"tier": "tier3", "label": "Tier 3", "default": True, "cap": cap}],
        "exception_approver": "manager",
    }
    if basis is not None:
        definition["basis"] = basis
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.hotel,
        source_clause_ref="HOTEL-CAP",
        status=PolicyRuleStatus.active,
        definition=definition,
    )


def _hotel_cap(item: EvalItem, rule: PolicyRule) -> Any:
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=[rule]):
        verdict = PolicyEvalTool().evaluate(item, context=_ctx(), session=None)  # type: ignore[arg-type]
    caps = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
    assert len(caps) == 1, verdict.verdicts
    return caps[0]


def _item(fields: dict) -> EvalItem:
    return build_eval_item_from_fields(
        fields, category="lodging", ref_id="hyatt", provenance={"vendor": DET}
    )


# ---------------------------------------------------------------------------
# 1. The canonical rule really is room_plus_tax
# ---------------------------------------------------------------------------


def test_canonical_hotel_cap_basis_is_room_plus_tax() -> None:
    assert HOTEL_CAP_DEFINITION["basis"] == "room_plus_tax"


# ---------------------------------------------------------------------------
# 2. EvalItem figures built from the receipt fields
# ---------------------------------------------------------------------------


class TestFieldsFigures:
    def test_hyatt_nightly_is_total_over_nights_not_room_rate(self) -> None:
        item = _item(HYATT)
        assert item.amount_usd == pytest.approx(250.0)
        assert item.nights == 3
        # The room-only figure is kept aside for a legacy room_only cap.
        assert item.room_only_nightly_usd == pytest.approx(230.0)

    def test_room_only_figure_needs_a_subtotal(self) -> None:
        fields = {k: v for k, v in HYATT.items() if k not in ("subtotal", "tax")}
        item = _item(fields)
        assert item.amount_usd == pytest.approx(250.0)
        assert item.room_only_nightly_usd is None

    @pytest.mark.parametrize("subtotal", [760.0, 0.0, -5.0, "abc"])
    def test_inconsistent_subtotal_is_ignored(self, subtotal: Any) -> None:
        item = _item({**HYATT, "subtotal": subtotal})
        assert item.amount_usd == pytest.approx(250.0)
        assert item.room_only_nightly_usd is None

    def test_no_nights_rate_divides_subtotal_implies_nights(self) -> None:
        """690 / 230 = 3 nights -> 750 / 3 = 250 (never the 230 room rate)."""
        fields = {k: v for k, v in HYATT.items() if k != "nights"}
        item = _item(fields)
        assert item.nights is None
        assert item.amount_usd == pytest.approx(250.0)
        assert item.amount_is_nightly_rate is True
        assert item.stay_amount_usd == pytest.approx(750.0)
        assert item.room_only_nightly_usd == pytest.approx(230.0)

    def test_no_nights_rate_divides_total_keeps_rate(self) -> None:
        """No subtotal: 1485 = 3 x 495 -> the rate already covers everything."""
        item = _item({"amount": 1485.0, "per_night_rate": 495.0})
        assert item.amount_usd == pytest.approx(495.0)
        assert item.amount_is_nightly_rate is True
        assert item.stay_amount_usd == pytest.approx(1485.0)
        assert item.room_only_nightly_usd is None

    def test_no_nights_rate_divides_nothing_fails_closed_to_line_amount(self) -> None:
        """750 is not a multiple of 230 and no subtotal: compare the full 750."""
        item = _item({"amount": 750.0, "per_night_rate": 230.0, "tax": 60.0})
        assert item.amount_usd == pytest.approx(750.0)
        assert item.amount_is_nightly_rate is False
        assert item.room_only_nightly_usd is None

    @pytest.mark.parametrize(
        "fields",
        [
            {"amount": 750.0, "per_night_rate": 1.0},  # 750 "nights" at $1
            {"amount": 750.0, "per_night_rate": 0.75},
            {"amount": 3100.0, "per_night_rate": 100.0},  # 31 nights: beyond the bound
        ],
    )
    def test_implausible_implied_stay_fails_closed_to_line_amount(
        self, fields: dict[str, float]
    ) -> None:
        """Security review: an OCR per-night rate that implies an implausible stay
        (more than MAX_IMPLIED_NIGHTS) must never shrink the nightly figure."""
        item = _item(fields)
        assert item.amount_usd == pytest.approx(fields["amount"])
        assert item.amount_is_nightly_rate is False

    def test_thirty_night_stay_is_still_implied(self) -> None:
        item = _item({"amount": 3000.0, "per_night_rate": 100.0})
        assert item.amount_usd == pytest.approx(100.0)

    def test_rate_above_line_amount_fails_closed(self) -> None:
        item = _item({"amount": 200.0, "per_night_rate": 230.0})
        assert item.amount_usd == pytest.approx(200.0)
        assert item.amount_is_nightly_rate is False

    @pytest.mark.parametrize(
        "provenance",
        [
            {"amount": "user_edited"},
            {"amount": "ai_inferred"},
            {"amount": DET, "subtotal": "user_edited", "per_night_rate": "user_edited"},
        ],
    )
    def test_receipt_router_path_drops_untrusted_room_extras(self, provenance: dict) -> None:
        """Upload / correction path: extras only describe an OCR-read total."""
        fields = {k: v for k, v in HYATT.items() if k != "nights"}
        item = build_eval_item_from_fields(
            fields, category="lodging", ref_id="hyatt", provenance=provenance
        )
        assert item.amount_usd == pytest.approx(750.0)
        assert item.amount_is_nightly_rate is False
        assert item.room_only_nightly_usd is None

    def test_never_below_line_amount_over_nights(self) -> None:
        """A tiny OCR per-night rate can never pull the figure below total / nights."""
        item = _item({**HYATT, "per_night_rate": 10.0})
        assert item.amount_usd == pytest.approx(250.0)


# ---------------------------------------------------------------------------
# 3. PolicyEvalTool honours the rule's basis
# ---------------------------------------------------------------------------


class TestEvaluatorBasis:
    def test_room_plus_tax_compares_250(self) -> None:
        v = _hotel_cap(_item(HYATT), _cap_rule("room_plus_tax"))
        assert v.in_policy is False
        assert v.observed["amount_usd"] == pytest.approx(250.0)
        assert "Nightly rate 250.00 USD (3 nights)" in v.reason, v.reason
        assert "230.00" not in v.reason

    @pytest.mark.parametrize("basis", [None, "room_and_tax", "", 7])
    def test_absent_or_unknown_basis_fails_closed_to_room_plus_tax(self, basis: Any) -> None:
        v = _hotel_cap(_item(HYATT), _cap_rule(basis))
        assert v.in_policy is False
        assert v.observed["amount_usd"] == pytest.approx(250.0)

    def test_room_only_compares_subtotal_over_nights(self) -> None:
        v = _hotel_cap(_item(HYATT), _cap_rule("room_only"))
        assert v.in_policy is True
        assert v.observed["amount_usd"] == pytest.approx(230.0)
        assert v.observed["basis"] == "room_only"

    def test_room_only_violation_wording_says_room_only(self) -> None:
        v = _hotel_cap(_item(HYATT), _cap_rule("room_only", cap=200.0))
        assert v.in_policy is False
        assert v.observed["amount_usd"] == pytest.approx(230.0)
        assert "Nightly room rate 230.00 USD excl. tax (3 nights)" in v.reason, v.reason

    def test_room_only_without_subtotal_uses_line_amount(self) -> None:
        fields = {k: v for k, v in HYATT.items() if k not in ("subtotal", "tax")}
        v = _hotel_cap(_item(fields), _cap_rule("room_only"))
        assert v.in_policy is False
        assert v.observed["amount_usd"] == pytest.approx(250.0)
        assert "basis" not in v.observed

    def test_room_only_figure_above_amount_is_ignored(self) -> None:
        """A (what-if) room-only figure above the room+tax figure never applies."""
        item = EvalItem(item_type="hotel", amount_usd=250.0, nights=3, room_only_nightly_usd=300.0)
        v = _hotel_cap(item, _cap_rule("room_only", cap=260.0))
        assert v.in_policy is True
        assert v.observed["amount_usd"] == pytest.approx(250.0)

    def test_seeded_rule_set_uses_room_plus_tax(self) -> None:
        """The REAL seeded HOTEL-CAP (autouse fixture) compares 250 for the Hyatt."""
        verdict = PolicyEvalTool().evaluate(_item(HYATT), context=_ctx(), session=None)  # type: ignore[arg-type]
        cap = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(250.0)


# ---------------------------------------------------------------------------
# 4. Entry / approval-time path (receipt evidence)
# ---------------------------------------------------------------------------


class TestEntryPath:
    def test_presubmit_hyatt_reports_250_per_night(self) -> None:
        s = _session_with_users()
        rec = _receipt(s, None, fields=dict(HYATT), provenance=dict(HYATT_PROV))
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging", amount=750.0, vendor="Hyatt Regency", primary_receipt_id=rec.id
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap, [f.reason for f in flags]
        assert cap[0].observed["amount_usd"] == pytest.approx(250.0)
        assert "Nightly rate 250.00 USD (3 nights)" in cap[0].reason, cap[0].reason

    def test_approval_time_hyatt_reports_250_per_night(self) -> None:
        s = _session_with_users()
        r = _report(s, total="750.00")
        e = _entry(s, r, category="lodging", amount="750.00", vendor="Hyatt Regency")
        _receipt(s, e, fields=dict(HYATT), provenance=dict(HYATT_PROV))
        a, _ = _assess(s, r)
        cap = _violations(a, "HOTEL-CAP")
        assert cap, a.flags_snapshot
        assert cap[0]["observed"]["amount_usd"] == pytest.approx(250.0)

    def _room_only_rules(self) -> list[PolicyRule]:
        rule = _cap_rule("room_only")
        return [rule]

    def test_entry_room_only_uses_trusted_subtotal(self) -> None:
        s = _session_with_users()
        rec = _receipt(s, None, fields=dict(HYATT), provenance=dict(HYATT_PROV))
        with patch.object(
            PolicyEvalTool, "_load_active_rules", return_value=self._room_only_rules()
        ):
            flags = _presubmit(
                s,
                _draft_entry(
                    category="lodging",
                    amount=750.0,
                    vendor="Hyatt Regency",
                    primary_receipt_id=rec.id,
                ),
            )
        assert not _pv(flags, "HOTEL-CAP"), [f.reason for f in flags]

    @pytest.mark.parametrize(
        "provenance",
        [
            {**HYATT_PROV, "subtotal": "user_edited"},
            {**HYATT_PROV, "subtotal": "ai_inferred"},
            {**HYATT_PROV, "amount": "user_edited"},
        ],
    )
    def test_entry_room_only_ignores_untrusted_subtotal(self, provenance: dict) -> None:
        s = _session_with_users()
        rec = _receipt(s, None, fields=dict(HYATT), provenance=provenance)
        with patch.object(
            PolicyEvalTool, "_load_active_rules", return_value=self._room_only_rules()
        ):
            flags = _presubmit(
                s,
                _draft_entry(
                    category="lodging",
                    amount=750.0,
                    vendor="Hyatt Regency",
                    primary_receipt_id=rec.id,
                ),
            )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(250.0)

    def test_entry_room_only_ignores_subtotal_when_claim_differs(self) -> None:
        """The claim (900) no longer equals the OCR total (750): no room-only figure."""
        s = _session_with_users()
        rec = _receipt(s, None, fields=copy.deepcopy(HYATT), provenance=dict(HYATT_PROV))
        with patch.object(
            PolicyEvalTool, "_load_active_rules", return_value=self._room_only_rules()
        ):
            flags = _presubmit(
                s,
                _draft_entry(
                    category="lodging",
                    amount=900.0,
                    vendor="Hyatt Regency",
                    primary_receipt_id=rec.id,
                ),
            )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(300.0)
