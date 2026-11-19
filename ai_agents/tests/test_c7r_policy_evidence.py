"""C7R regressions — approval-time / pre-submit policy flags built from TRUSTED evidence.

Staging re-acceptance (C7R) found false / misleading policy flags:

  a) a manual 'United Airlines' 320.00 airfare WITH an attached receipt whose
     deterministic OCR vendor is 'UNITED' raised a BLOCKING AIR-PREF
     "Supplier '' is NOT in the preferred list" — the claim-vs-receipt vendor
     comparison was an exact string match, so the trusted receipt was ignored
     and the evaluator printed an empty supplier;
  b) a manual 600.00 lodging entry was flagged "exceeds cap 180 (tier
     domestic, city None)" — no hint that the city / nights were simply not
     verifiable;
  c) RECEIPT-REQ reasons named an internal ref instead of the vendor.

Hermetic: reuses the seeded-rule FakeSession harness of
``test_c7_05_15_policy_gates`` (real ``database.seeds.policy_content.RULES``,
no DB, no LLM), so every case runs the production chain
    Expense rows -> DraftEntry -> run_presubmit(strict) -> PolicyEvalTool -> flags
    -> RiskAssessment -> decide_route.

The C7-05 / C7-15 trust boundary is re-asserted here: claimant-typed vendor /
city never clear a rule; only deterministic receipt OCR fields and owned card
merchants do; a manual airfare without such evidence ALWAYS escalates.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    build_entry_eval_item,
    build_eval_item_from_fields,
    card_merchant_brand,
    entry_label,
    same_supplier,
)
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    EMPLOYEE,
    ORG,
    FakeSession,
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
    _trip_with_segments,
    _violations,
)
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, supplier_matches_preferred
from database.models.card_transaction import CardTransaction

DET = "deterministic"
PREFERRED_AIR = ["United Airlines", "Delta Air Lines", "Lufthansa"]
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _card(s: FakeSession, expense: Any, *, merchant: str, amount: str) -> CardTransaction:
    return s.add_obj(
        CardTransaction(
            id=uuid.uuid4(),
            organization_id=ORG,
            user_id=EMPLOYEE,
            expense_id=expense.id if expense is not None else None,
            merchant=merchant,
            amount=Decimal(amount),
            currency="USD",
            txn_date=date(2026, 6, 10),
        )
    )


def _reasons(a: Any) -> list[str]:
    return [str(f.get("reason")) for f in a.flags_snapshot if isinstance(f, dict)]


def _blocking(a: Any, clause: str) -> list[dict]:
    return [
        f
        for f in a.flags_snapshot
        if f.get("source_clause_ref") == clause and f.get("severity") == "blocking"
    ]


# ---------------------------------------------------------------------------
# 1. Robust supplier matching (both directions)
# ---------------------------------------------------------------------------


class TestSupplierMatching:
    @pytest.mark.parametrize(
        "name",
        [
            "UNITED",
            "United Airlines",
            "UNITED AIRLINES INC",
            "United Air Lines, Inc.",
            "united airlines corp",
            "UNITED AIRWAYS CO",
            "  united   airlines  ",
        ],
    )
    def test_united_variants_match_preferred(self, name: str) -> None:
        assert supplier_matches_preferred(name, PREFERRED_AIR)
        assert same_supplier(name, "United Airlines")

    @pytest.mark.parametrize(
        "name", ["SKYBRIDGE AIR", "UNITED BISTRO", "UNITEDX", "United Nigeria Airlines", ""]
    )
    def test_lookalikes_do_not_match(self, name: str) -> None:
        assert not supplier_matches_preferred(name, PREFERRED_AIR)
        assert not same_supplier(name, "United Airlines")

    def test_delta_air_lines_spacing(self) -> None:
        assert supplier_matches_preferred("DELTA AIRLINES", PREFERRED_AIR)
        assert same_supplier("DELTA AIR LINES INC", "Delta Air Lines")


# ---------------------------------------------------------------------------
# 2. Staging (a) — receipt-attached manual airfare
# ---------------------------------------------------------------------------


class TestReceiptAttachedAirfare:
    @pytest.mark.parametrize("ocr_vendor", ["UNITED", "UNITED AIRLINES INC"])
    def test_manual_united_with_deterministic_receipt_clears_air_pref(
        self, ocr_vendor: str
    ) -> None:
        """Staging (a): 'United Airlines' 320 + receipt OCR 'UNITED' 320 -> no AIR-PREF."""
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            # Dated on the claimed day (the _entry default): a receipt dated far
            # from the claim is an escalating receipt_mismatch flag (UAT2).
            fields={"vendor": ocr_vendor, "amount": 320.0, "date": "2026-11-01"},
            provenance={"vendor": DET, "amount": DET, "date": DET},
        )
        a, route = _assess(s, r)
        assert not _blocking(a, "AIR-PREF"), a.flags_snapshot
        assert not any("Supplier ''" in x for x in _reasons(a)), _reasons(a)
        assert a.is_policy_compliant is True, a.policy_rationale
        assert route == "auto_approve"

    def test_blank_claim_vendor_uses_receipt_vendor(self) -> None:
        s = _session_with_users()
        rec = _receipt(
            s, None, fields={"vendor": "UNITED", "amount": 320.0}, provenance={"vendor": DET}
        )
        item = build_entry_eval_item(
            _draft_entry(category="airfare", amount=320.0, vendor=None, primary_receipt_id=rec.id),
            idx=0,
            context=_ctx(EMPLOYEE),
            session=s,  # type: ignore[arg-type]
            owner_id=EMPLOYEE,
        )
        assert item.supplier == "UNITED"

    def test_skybridge_receipt_names_the_real_supplier(self) -> None:
        """A genuine non-preferred carrier is a real violation naming the carrier."""
        s = _session_with_users()
        r = _report(s, total="450.00")
        e = _entry(s, r, category="airfare", amount="450.00", vendor="SKYBRIDGE AIR")
        _receipt(
            s,
            e,
            fields={"vendor": "SKYBRIDGE AIR", "amount": 450.0},
            provenance={"vendor": DET, "amount": DET},
        )
        a, route = _assess(s, r)
        v = _violations(a, "AIR-PREF")
        assert v and "SKYBRIDGE AIR" in v[0]["reason"], a.flags_snapshot
        assert "''" not in v[0]["reason"]
        assert route == "escalate"

    @pytest.mark.parametrize("ocr_vendor", ["UNITEDX", "UNITED BISTRO", "SKYBRIDGE AIR"])
    def test_receipt_vendor_contradicting_claim_escalates_naming_both(
        self, ocr_vendor: str
    ) -> None:
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": ocr_vendor, "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        a, route = _assess(s, r)
        v = _blocking(a, "AIR-PREF")
        assert v, a.flags_snapshot
        assert "United Airlines" in v[0]["reason"] and ocr_vendor in v[0]["reason"]
        assert "Supplier ''" not in v[0]["reason"]
        assert route == "escalate"

    def test_receipt_and_card_disagreeing_escalates(self) -> None:
        """Two trusted records that disagree never verify the supplier."""
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": "UNITED", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        _card(s, e, merchant="SKYBRIDGE AIR", amount="320.00")
        a, route = _assess(s, r)
        assert _blocking(a, "AIR-PREF"), a.flags_snapshot
        assert route == "escalate"

    def test_card_merchant_variant_verifies_supplier(self) -> None:
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _card(s, e, merchant="UNITED AIRLINES INC", amount="300.00")
        _receipt(
            s,
            e,
            fields={"vendor": "UNITED", "amount": 300.0},
            provenance={"vendor": DET, "amount": DET},
        )
        a, route = _assess(s, r)
        assert not _blocking(a, "AIR-PREF"), a.flags_snapshot
        assert route == "auto_approve"


# ---------------------------------------------------------------------------
# 3. Unverifiable supplier — honest, still-blocking flag (trust boundary kept)
# ---------------------------------------------------------------------------


class TestUnverifiedSupplier:
    def test_manual_united_without_evidence_escalates_with_honest_reason(self) -> None:
        """Typed 'United Airlines', no receipt / card / segment -> ALWAYS escalate,
        but the reason states the supplier could not be verified (never "Supplier ''")."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        a, route = _assess(s, r)
        v = _blocking(a, "AIR-PREF")
        assert v, a.flags_snapshot
        reason = v[0]["reason"]
        assert "could not be verified" in reason and "'United Airlines'" in reason, reason
        assert "Supplier ''" not in reason
        assert v[0]["flag_type"] == "policy_violation"
        assert v[0]["observed"]["supplier_verified"] is False
        assert a.is_policy_compliant is False
        assert route == "escalate"

    @pytest.mark.parametrize("prov", ["ai_inferred", "user_edited"])
    def test_untrusted_receipt_vendor_does_not_verify(self, prov: str) -> None:
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": "UNITED", "amount": 320.0},
            provenance={"vendor": prov, "amount": DET},
        )
        a, route = _assess(s, r)
        v = _blocking(a, "AIR-PREF")
        assert v and "could not be verified" in v[0]["reason"], a.flags_snapshot
        assert route == "escalate"

    def test_no_vendor_at_all_still_blocks(self) -> None:
        s = _session_with_users()
        flags = _presubmit(s, _draft_entry(category="airfare", amount=320.0, vendor=None))
        v = _pv(flags, "AIR-PREF")
        assert v and "Supplier ''" not in v[0].reason, [f.reason for f in flags]
        assert "unknown" in v[0].reason.lower()

    def test_upload_path_untrusted_vendor_is_reported_as_claimed(self) -> None:
        item = build_eval_item_from_fields(
            {"vendor": "UNITED", "amount": 320.0},
            category="airfare",
            ref_id="r1",
            provenance={"vendor": "ai_inferred"},
        )
        assert item.supplier is None and item.claimed_supplier == "UNITED"
        verdict = PolicyEvalTool().evaluate(
            item,
            context=_ctx(),
            session=None,  # type: ignore[arg-type]
        )
        pref = [v for v in verdict.verdicts if v.source_clause_ref == "AIR-PREF"]
        assert pref and pref[0].in_policy is False
        assert "(claimed: 'UNITED')" in pref[0].reason

    def test_tool_blank_supplier_verdict_is_still_a_violation(self) -> None:
        """M-A3 gold label m_a3_flight_empty_supplier_violation must keep holding."""
        verdict = PolicyEvalTool().evaluate(
            EvalItem(item_type="flight", amount_usd=300.0, supplier=None),
            context=_ctx(),
            session=None,  # type: ignore[arg-type]
        )
        pref = [v for v in verdict.verdicts if v.source_clause_ref == "AIR-PREF"]
        assert pref and pref[0].in_policy is False
        assert "''" not in pref[0].reason


# ---------------------------------------------------------------------------
# 4. Staging (b) — lodging city / nights
# ---------------------------------------------------------------------------


class TestLodgingEvidence:
    def test_receipt_city_and_nights_select_tier1_nightly_cap(self) -> None:
        """600 for 3 nights in New York (deterministic receipt) = 200/night < 300."""
        s = _session_with_users()
        r = _report(s, total="600.00")
        e = _entry(s, r, category="lodging", amount="600.00", vendor="Marriott")
        _receipt(
            s,
            e,
            fields={"vendor": "MARRIOTT", "amount": 600.0, "location": "NEW YORK, NY", "nights": 3},
            provenance={"vendor": DET, "amount": DET, "location": DET},
        )
        a, route = _assess(s, r)
        assert not _blocking(a, "HOTEL-CAP"), a.flags_snapshot
        assert a.is_policy_compliant is True, a.policy_rationale
        assert route == "escalate"  # total 600 >= auto-approve threshold, but compliant

    def test_manual_lodging_without_evidence_is_honest_and_blocking(self) -> None:
        """Staging (b): city / nights unverifiable -> still blocking (lowest cap, full
        amount), but the reason says so instead of 'city None'."""
        s = _session_with_users()
        r = _report(s, total="600.00")
        _entry(s, r, category="lodging", amount="600.00", vendor="Marriott")
        a, route = _assess(s, r)
        v = _blocking(a, "HOTEL-CAP")
        assert v, a.flags_snapshot
        reason = v[0]["reason"]
        assert "None" not in reason, reason
        assert "city could not be verified" in reason
        assert "nights could not be verified" in reason
        assert "Tier 3" in reason and "lowest cap" in reason
        assert route == "escalate"

    def test_unverified_receipt_city_is_reported_as_claimed(self) -> None:
        s = _session_with_users()
        r = _report(s, total="600.00")
        e = _entry(s, r, category="lodging", amount="600.00", vendor="Marriott")
        _receipt(
            s,
            e,
            fields={"vendor": "MARRIOTT", "amount": 600.0, "location": "New York", "nights": 3},
            provenance={"vendor": DET, "amount": DET, "location": "user_edited"},
        )
        a, route = _assess(s, r)
        v = _violations(a, "HOTEL-CAP")
        assert v, a.flags_snapshot
        assert "(claimed: 'New York')" in v[0]["reason"], v[0]["reason"]
        # 3 OCR nights are still used: 200/night against the lowest (Tier 3) 160 cap.
        assert v[0]["observed"]["amount_usd"] == pytest.approx(200.0)
        assert v[0]["observed"]["cap"] == 160
        # Issue 6: within the CLAIMED city's Tier 1 cap (300) -> pending verification:
        # advisory for submit, but still a policy_violation that escalates.
        assert v[0]["pending_verification"] == "city"
        assert v[0]["severity"] == "advisory"
        assert "within the Tier 1 New York cap of $300/night" in v[0]["reason"]
        assert route == "escalate"


# ---------------------------------------------------------------------------
# 5. Staging (c) — reasons name the vendor / category + amount + date
# ---------------------------------------------------------------------------


class TestReasonLabels:
    def test_entry_label(self) -> None:
        with_vendor = _draft_entry(category="airfare", amount=320.0, vendor=" SKYBRIDGE AIR ")
        assert entry_label(with_vendor) == "SKYBRIDGE AIR"
        no_vendor = DraftEntry(
            category="lodging",
            amount=600.0,
            currency="USD",
            expense_date="2026-06-10",
            vendor=None,
            description=None,
            status="draft",
            match_source=None,
            trip_segment_id=None,
            is_itemized=False,
            primary_receipt_id=None,
            source_card_txn_id=None,
            sibling_group_id=None,
        )
        assert entry_label(no_vendor) == "Lodging 600.00 USD on 2026-06-10"

    def test_missing_receipt_reason_names_category_amount_date(self) -> None:
        s = _session_with_users()
        flags = _presubmit(s, _draft_entry(category="lodging", amount=150.0, vendor=None))
        missing = [f for f in flags if f.flag_type == "missing_receipt"]
        assert missing, [f.reason for f in flags]
        assert "Lodging 150.00 USD on 2026-06-10" in missing[0].reason
        assert "entry_0" not in missing[0].reason
        assert not _UUID_RE.search(missing[0].reason)

    def test_missing_receipt_reason_names_vendor(self) -> None:
        s = _session_with_users()
        r = _report(s, total="450.00")
        _entry(s, r, category="airfare", amount="450.00", vendor="SKYBRIDGE AIR")
        a, _ = _assess(s, r)
        missing = [f for f in a.flags_snapshot if f.get("flag_type") == "missing_receipt"]
        assert missing and "Receipt required for SKYBRIDGE AIR" in missing[0]["reason"]
        assert not any(_UUID_RE.search(x) for x in _reasons(a)), _reasons(a)


# ---------------------------------------------------------------------------
# 6. Review round — per-night wording, card descriptors, approved segments
# ---------------------------------------------------------------------------


def _hotel_cap_reason(item: EvalItem) -> str:
    verdict = PolicyEvalTool().evaluate(
        item,
        context=_ctx(),
        session=None,  # type: ignore[arg-type]
    )
    cap = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
    assert cap and cap[0].in_policy is False, [v.reason for v in verdict.verdicts]
    return cap[0].reason


class TestPerNightRateWording:
    """The receipt's per-night rate (no nights) is never called the full amount."""

    def test_per_night_rate_without_nights_is_described_as_nightly(self) -> None:
        item = build_eval_item_from_fields(
            {"amount": 1485.0, "per_night_rate": 495.0, "vendor": "Marriott"},
            category="lodging",
            ref_id="x",
            provenance={"vendor": DET},
        )
        assert item.amount_usd == pytest.approx(495.0) and item.nights is None
        reason = _hotel_cap_reason(item)
        assert "Nightly rate 495.00 USD" in reason, reason
        assert "stay total 1485.00 USD" in reason, reason
        assert "Hotel amount" not in reason
        assert "full amount was compared" not in reason

    def test_full_amount_without_nights_still_says_so(self) -> None:
        item = build_eval_item_from_fields(
            {"amount": 600.0, "vendor": "Marriott"},
            category="lodging",
            ref_id="x",
            provenance={"vendor": DET},
        )
        assert item.amount_is_nightly_rate is False
        reason = _hotel_cap_reason(item)
        assert "Hotel amount 600.00 USD" in reason
        assert "full amount was compared" in reason

    def test_entry_path_receipt_per_night_rate(self) -> None:
        """Approval-time / /policy/evaluate builder: OCR per-night rate, no OCR nights."""
        s = _session_with_users()
        r = _report(s, total="1485.00")
        e = _entry(s, r, category="lodging", amount="1485.00", vendor="Marriott")
        _receipt(
            s,
            e,
            fields={"vendor": "MARRIOTT", "amount": 1485.0, "per_night_rate": 495.0},
            provenance={"vendor": DET, "amount": DET},
        )
        a, route = _assess(s, r)
        v = _blocking(a, "HOTEL-CAP")
        assert v, a.flags_snapshot
        reason = v[0]["reason"]
        assert "Nightly rate 495.00 USD" in reason and "full amount" not in reason, reason
        assert route == "escalate"


class TestCardMerchantDescriptor:
    @pytest.mark.parametrize(
        ("merchant", "brand"),
        [
            ("UNITED AIRLINES 0162345678 HOUSTON TX", "UNITED AIRLINES"),
            ("UNITED 016-2345678", "UNITED"),
            ("United Airlines", "United Airlines"),
            ("UNITED NIGERIA AIRLINES", "UNITED NIGERIA AIRLINES"),
            ("0162345678 UNITED", "0162345678 UNITED"),
        ],
    )
    def test_brand_extraction(self, merchant: str, brand: str) -> None:
        assert card_merchant_brand(merchant) == brand

    def test_receipt_plus_padded_card_descriptor_verifies(self) -> None:
        """Review finding: a real card descriptor (ticket no. + city) must not read as a
        conflict with the receipt's 'UNITED'."""
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": "UNITED", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        _card(s, e, merchant="UNITED AIRLINES 0162345678 HOUSTON TX", amount="320.00")
        a, route = _assess(s, r)
        assert not _blocking(a, "AIR-PREF"), a.flags_snapshot
        assert route == "auto_approve"

    def test_card_only_padded_descriptor_verifies(self) -> None:
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"amount": 320.0},
            provenance={"amount": DET},
        )
        _card(s, e, merchant="UNITED AIRLINES 0162345678 HOUSTON TX", amount="320.00")
        a, _ = _assess(s, r)
        assert not _blocking(a, "AIR-PREF"), a.flags_snapshot

    def test_lookalike_card_descriptor_still_conflicts(self) -> None:
        s = _session_with_users()
        r = _report(s, total="320.00")
        e = _entry(s, r, category="airfare", amount="320.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": "UNITED", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        _card(s, e, merchant="UNITED NIGERIA AIRLINES 0162345678 LAGOS", amount="320.00")
        a, route = _assess(s, r)
        assert _blocking(a, "AIR-PREF"), a.flags_snapshot
        assert route == "escalate"


class TestApprovedSegmentEvidence:
    def test_segment_supplier_confirmed_by_normalized_receipt_vendor(self) -> None:
        """Approved segment 'United Airlines' + deterministic receipt 'UNITED' confirm the
        segment supplier (normalized match), even though the typed vendor says 'Delta'."""
        s = _session_with_users()
        flight, _ = _trip_with_segments(s)
        rec = _receipt(
            s,
            None,
            fields={"vendor": "UNITED", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        item = build_entry_eval_item(
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="Delta",
                trip_segment_id=flight.id,
                primary_receipt_id=rec.id,
            ),
            idx=0,
            context=_ctx(EMPLOYEE),
            session=s,  # type: ignore[arg-type]
            owner_id=EMPLOYEE,
        )
        assert item.supplier == "United Airlines", item

    def test_segment_supplier_contradicted_by_receipt_is_dropped(self) -> None:
        s = _session_with_users()
        flight, _ = _trip_with_segments(s)
        rec = _receipt(
            s,
            None,
            fields={"vendor": "SKYBRIDGE AIR", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=flight.id,
                primary_receipt_id=rec.id,
            ),
        )
        v = _pv(flags, "AIR-PREF")
        assert v and "SKYBRIDGE AIR" in v[0].reason, [f.reason for f in flags]
        assert "Supplier ''" not in v[0].reason

    def test_segment_city_dropped_on_conflict_reports_claimed_city(self) -> None:
        """Segment says New York, the receipt deterministically says Boston: city
        unknown -> domestic cap, and the reason names the claimed (segment) city."""
        s = _session_with_users()
        _, hotel = _trip_with_segments(s)
        rec = _receipt(
            s,
            None,
            fields={"vendor": "MARRIOTT", "amount": 956.0, "location": "BOSTON, MA"},
            provenance={"vendor": DET, "amount": DET, "location": DET},
        )
        entry = _draft_entry(
            category="lodging",
            amount=956.0,
            vendor="Marriott",
            trip_segment_id=hotel.id,
            primary_receipt_id=rec.id,
        )
        item = build_entry_eval_item(
            entry,
            idx=0,
            context=_ctx(EMPLOYEE),
            session=s,  # type: ignore[arg-type]
            owner_id=EMPLOYEE,
        )
        assert item.city is None and item.claimed_city == "New York, NY", item
        reason = _hotel_cap_reason(item)
        assert "(claimed: 'New York, NY')" in reason, reason
        assert "None" not in reason
