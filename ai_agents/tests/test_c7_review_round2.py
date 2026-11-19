"""C7 whole-branch review, round 2 — hermetic regressions.

Reuses the seeded-rule FakeSession harness (real ``database.seeds.policy_content``
RULES, no DB, no LLM), so the approval cases run the production chain
    Expense rows → run_presubmit(strict) → PolicyEvalTool → flags → decide_route.

  F1  PATCH /receipts {vendor|location} overwrote the OCR value that contradicted
      an approved trip segment; the claimant-typed segment then won again
      (AIR-PREF / HOTEL-CAP bypass at approval time).
  F2  an AUTO-approved trip (moved to 'approved' with no human looking at the
      claimant-typed nights / city / supplier) — or an approved trip with no
      approval row at all — must not enrich entry evaluation.
  F3  RECEIPT-REQ was satisfied by ANY linked receipt id (a $3 coffee receipt
      attached to a $60 dinner / $450 hotel); the receipt must be the owner's and
      its machine-read amount must cover the claim.
  F4  a withheld needs_review policy draft must not leak the critic's rationale
      through ``critique`` in the API / SSE result frame.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from ai_agents.agents.policy_agent import presentable_policy_output
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    OTHER_EMPLOYEE,
    _assess,
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
from ai_agents.tests.test_c7_05_15_review_fixes import _card, _flight_seg, _hotel_seg, _trip
from ai_agents.tests.test_c7_g4_policy_qa_contract import _CITED_DRAFT, _retrieval, _run
from backend.app.modules.ai.streaming import build_sse_events
from database.models.approval import ApprovalStatus
from database.models.trip import TripStatus

DET = "deterministic"
EDITED = "user_edited"


def _missing(flags_or_snapshot: list[Any]) -> list[Any]:
    out = []
    for f in flags_or_snapshot:
        ft = f.get("flag_type") if isinstance(f, dict) else f.flag_type
        if ft == "missing_receipt":
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# F1 — a corrected receipt vendor / location cannot re-enable the segment value
# ---------------------------------------------------------------------------


class TestF1CorrectedReceiptEvidence:
    def test_corrected_vendor_on_approved_segment_escalates_at_approval(self) -> None:
        """Approved trip names United; the traveler flew SKYBRIDGE (OCR) and then
        PATCHed the receipt vendor.  Approval must escalate on AIR-PREF."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved), amount="450.00")
        r = _report(s, total="450.00")
        e = _entry(
            s,
            r,
            category="airfare",
            amount="450.00",
            vendor="SKYBRIDGE AIR",
            trip_segment_id=seg.id,
        )
        _receipt(
            s,
            e,
            fields={"vendor": "SKYBRIDGE AIR", "amount": 450.0},
            provenance={"vendor": EDITED, "amount": DET},
        )

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF"), a.flags_snapshot
        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_resending_the_same_vendor_is_enough_to_distrust_segment(self) -> None:
        """The correction need not change the value: provenance is what matters."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved))
        rec = _receipt(
            s,
            None,
            fields={"vendor": "United Airlines", "amount": 320.0},
            provenance={"vendor": EDITED, "amount": DET},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=seg.id,
                primary_receipt_id=rec.id,
            ),
        )
        assert _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_deterministic_receipt_vendor_matching_segment_still_clears(self) -> None:
        """Control: an unedited OCR vendor agreeing with the approved segment."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved))
        rec = _receipt(
            s,
            None,
            fields={"vendor": "United Airlines", "amount": 320.0},
            provenance={"vendor": DET, "amount": DET},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=seg.id,
                primary_receipt_id=rec.id,
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_card_merchant_confirms_segment_despite_edited_receipt_vendor(self) -> None:
        """Independent evidence (the owner's card merchant) still confirms the plan."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved))
        rec = _receipt(
            s,
            None,
            fields={"vendor": "United", "amount": 320.0},
            provenance={"vendor": EDITED, "amount": DET},
        )
        txn = _card(s, None, merchant="United Airlines", amount="320.00")
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=seg.id,
                primary_receipt_id=rec.id,
                source_card_txn_id=txn.id,
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_corrected_location_drops_segment_tier1_city_at_approval(self) -> None:
        """Approved segment says New York (Tier 1, 300); OCR said Albany; the traveler
        PATCHed the location.  The stay must be capped at the lowest (Tier 3) 160; the
        segment city is only a claim (pending verification, never auto-approved)."""
        s = _session_with_users()
        seg = _hotel_seg(
            s,
            _trip(s, status=TripStatus.approved),
            location="New York, NY",
            nights=1,
            amount="240.00",
        )
        r = _report(s, total="240.00")
        e = _entry(
            s, r, category="lodging", amount="240.00", vendor="Hilton", trip_segment_id=seg.id
        )
        _receipt(
            s,
            e,
            fields={"vendor": "Hilton", "amount": 240.0, "location": "New York, NY"},
            provenance={"vendor": DET, "amount": DET, "location": EDITED},
        )

        a, route = _assess(s, r)

        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["cap"] == 160, a.flags_snapshot
        assert cap[0]["pending_verification"] == "city"
        assert route == "escalate"


# ---------------------------------------------------------------------------
# F2 — only a HUMAN-approved trip's segments are trusted
# ---------------------------------------------------------------------------


class TestF2HumanApprovedTripOnly:
    @pytest.mark.parametrize("approval", [ApprovalStatus.auto_approved, None])
    @pytest.mark.parametrize("status", [TripStatus.approved, TripStatus.completed])
    def test_auto_or_unapproved_trip_segment_is_ignored(
        self, status: TripStatus, approval: ApprovalStatus | None
    ) -> None:
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=status, approval=approval))
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="United Airlines", trip_segment_id=seg.id
            ),
        )
        assert _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_auto_approved_trip_declared_nights_do_not_deflate_rate(self) -> None:
        """Reviewer probe: 450 hotel declared as 5 nights in New York, auto-approved;
        a 450 one-night stay linked to it read as 90/night.  Must be 450 at the lowest
        (Tier 3) 160 — the auto-approved plan's 5 nights / New York are never used."""
        s = _session_with_users()
        seg = _hotel_seg(
            s,
            _trip(s, status=TripStatus.approved, approval=ApprovalStatus.auto_approved),
            location="New York, NY",
            nights=5,
            amount="450.00",
        )
        r = _report(s, total="450.00")
        e = _entry(
            s, r, category="lodging", amount="450.00", vendor="Hilton", trip_segment_id=seg.id
        )
        _receipt(
            s,
            e,
            fields={"vendor": "Hilton", "amount": 450.0, "location": "Albany, NY", "nights": 1},
            provenance={"vendor": DET, "amount": DET, "location": "ai_inferred"},
        )

        a, route = _assess(s, r)

        cap = _violations(a, "HOTEL-CAP")
        assert cap, a.flags_snapshot
        assert cap[0]["observed"]["amount_usd"] == pytest.approx(450.0)
        assert cap[0]["observed"]["cap"] == 160
        assert cap[0]["severity"] == "blocking"
        assert cap[0]["pending_verification"] is None
        assert route == "escalate"

    def test_rejected_approval_row_is_not_trust(self) -> None:
        s = _session_with_users()
        seg = _flight_seg(
            s, _trip(s, status=TripStatus.approved, approval=ApprovalStatus.changes_requested)
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="United Airlines", trip_segment_id=seg.id
            ),
        )
        assert _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_human_approved_trip_segment_is_used(self) -> None:
        """Control: a human-approved plan still enriches the entry."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved, approval=ApprovalStatus.approved))
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="United Airlines", trip_segment_id=seg.id
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]


# ---------------------------------------------------------------------------
# F3 — RECEIPT-REQ needs a receipt that actually backs the claim
# ---------------------------------------------------------------------------


class TestF3ReceiptMustBackTheClaim:
    def test_unrelated_small_receipt_does_not_satisfy_receipt_req_at_approval(self) -> None:
        """A $3 coffee receipt attached to a $60 dinner → blocking missing_receipt."""
        s = _session_with_users()
        r = _report(s, total="60.00")
        e = _entry(s, r, category="meals", amount="60.00", vendor="Bistro")
        _receipt(
            s,
            e,
            fields={"vendor": "Coffee Cart", "amount": 3.0},
            provenance={"vendor": DET, "amount": DET},
        )

        a, route = _assess(s, r)

        missing = _missing(a.flags_snapshot)
        assert missing and missing[0]["severity"] == "blocking", a.flags_snapshot
        assert "does not cover" in missing[0]["reason"]
        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_matching_receipt_satisfies_receipt_req_and_auto_approves(self) -> None:
        """Control: the same dinner with its own receipt auto-approves."""
        s = _session_with_users()
        r = _report(s, total="60.00")
        e = _entry(s, r, category="meals", amount="60.00", vendor="Bistro")
        _receipt(
            s,
            e,
            fields={"vendor": "Bistro", "amount": 60.0},
            provenance={"vendor": DET, "amount": DET},
        )

        a, route = _assess(s, r)

        assert not _missing(a.flags_snapshot), a.flags_snapshot
        assert route == "auto_approve"

    def test_tip_on_top_of_receipt_total_is_within_slack(self) -> None:
        s = _session_with_users()
        rec = _receipt(s, None, fields={"amount": 50.0}, provenance={"amount": DET})
        flags = _presubmit(
            s,
            _draft_entry(category="meals", amount=60.0, vendor="Bistro", primary_receipt_id=rec.id),
        )
        assert not _missing(flags), [f.reason for f in flags]

    def test_user_edited_receipt_amount_does_not_back_the_claim(self) -> None:
        s = _session_with_users()
        rec = _receipt(s, None, fields={"amount": 60.0}, provenance={"amount": EDITED})
        flags = _presubmit(
            s,
            _draft_entry(category="meals", amount=60.0, vendor="Bistro", primary_receipt_id=rec.id),
        )
        assert _missing(flags), [f.reason for f in flags]

    def test_other_users_receipt_does_not_back_the_claim(self) -> None:
        s = _session_with_users()
        rec = _receipt(
            s,
            None,
            fields={"amount": 60.0},
            provenance={"amount": DET},
            uploaded_by=OTHER_EMPLOYEE,
        )
        flags = _presubmit(
            s,
            _draft_entry(category="meals", amount=60.0, vendor="Bistro", primary_receipt_id=rec.id),
        )
        assert _missing(flags), [f.reason for f in flags]

    def test_unknown_receipt_id_does_not_back_the_claim(self) -> None:
        s = _session_with_users()
        flags = _presubmit(
            s,
            _draft_entry(
                category="meals", amount=60.0, vendor="Bistro", primary_receipt_id=uuid.uuid4()
            ),
        )
        assert _missing(flags), [f.reason for f in flags]


# ---------------------------------------------------------------------------
# F4 — withheld policy draft: no critique side channel
# ---------------------------------------------------------------------------


class TestF4WithheldCritique:
    def test_withheld_output_nulls_critique(self) -> None:
        out, cits = presentable_policy_output(
            {
                "answer_type": "needs_review",
                "answer": None,
                "needs_review": True,
                "critique": "Last critique: the draft claims a 300 USD cap",
                "answer_structured": {"full_text": "withheld draft"},
            },
            (),
        )
        assert out["critique"] is None
        assert "300 USD" not in json.dumps(out)

    def test_grounded_output_keeps_critique(self) -> None:
        output = {"answer_type": "grounded", "answer": "x", "critique": "ok"}
        out, _ = presentable_policy_output(output, ())
        assert out == output

    def test_sse_result_frame_has_no_critique_for_withheld_agent_result(self) -> None:
        result, _ = _run(_retrieval(0.60), _CITED_DRAFT, "UNSUPPORTED\nthe draft claims 250.")
        assert result.output.get("answer") is None
        events = build_sse_events(
            result,
            task_type="policy_qa",
            route="policy",
            confidence=0.5,
            explain_with_llm=False,
            message_id=None,
            sequence=1,
        )
        frame = next(e for e in events if e.startswith("event: result"))
        payload = json.loads(frame.split("data: ", 1)[1])
        assert payload["output"].get("critique") is None
        assert payload["citations"] == []
        assert "the draft claims" not in frame
