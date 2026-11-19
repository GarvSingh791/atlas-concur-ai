"""C7-05 / C7-15 review-fix regressions (adversarial review of G2-policy-gates).

Hermetic: reuses the seeded-rule FakeSession harness from
``test_c7_05_15_policy_gates`` (real ``database.seeds.policy_content.RULES``,
no DB, no LLM), so every case runs the production chain
    Expense rows → DraftEntry → run_presubmit(strict) → PolicyEvalTool → flags
    → RiskAssessment → decide_route.

Covers the review findings:
  R1  claimant-typed trip segments (DRAFT trip via POST /trips) must not clear
      AIR-PREF / lift HOTEL-CAP; even an approved segment loses to conflicting
      receipt / card evidence.
  R2  receipt location / nights / per-night rate are provenance-gated
      (user_edited / ai_inferred location ≠ evidence).
  R3  trip approvals re-run PolicyEvalTool over the declared segments; the
      client-supplied in_policy / estimated_cost cannot alone auto-approve.
  R4  a stored advisory requires_approver flag on an otherwise clean report
      still escalates (was only guarded by a vacuous DB test).
  R5  bypass closures that had no test: stale per-night rate on an edited
      amount, another user's receipt / card txn, merchant ≠ vendor.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from ai_agents.agents.approval_agent.evaluate import RiskAssessment, assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    EMPLOYEE,
    MANAGER,
    ORG,
    OTHER_EMPLOYEE,
    FakeSession,
    _assess,
    _ctx,
    _draft_entry,
    _entry,
    _presubmit,
    _pv,
    _receipt,
    _report,
    _Rows,
    _seeded_rules,
    _session_with_users,
    _trip_approval,
    _violations,
)
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.card_transaction import CardTransaction
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

DET = "deterministic"


class TripFakeSession(FakeSession):
    """FakeSession that also answers the trip-segment select."""

    def scalars(self, stmt: Any) -> _Rows:
        sql = str(stmt)
        if "FROM trip_segments" in sql:
            params = stmt.compile().params
            trip_ids = {v for k, v in params.items() if k.startswith("trip_id")}
            return _Rows(
                [
                    o
                    for (m, _), o in self.objects.items()
                    if m is TripSegment and o.trip_id in trip_ids
                ]
            )
        return super().scalars(stmt)


def _trip_session() -> TripFakeSession:
    s = TripFakeSession()
    s.add_obj(User(id=EMPLOYEE, organization_id=ORG, manager_id=MANAGER, email="e@x"))
    s.add_obj(User(id=OTHER_EMPLOYEE, organization_id=ORG, manager_id=None, email="o@x"))
    return s


def _trip(
    s: FakeSession,
    *,
    status: TripStatus,
    estimated_cost: str | None = None,
    currency: str = "USD",
    approval: ApprovalStatus | None = ApprovalStatus.approved,
) -> Trip:
    """A trip; approved / completed ones get a HUMAN-approved approval row by
    default (pass ``approval=`` to model an auto-approved / approval-less trip)."""
    trip = s.add_obj(
        Trip(
            id=uuid.uuid4(),
            organization_id=ORG,
            traveler_id=EMPLOYEE,
            origin="SFO",
            primary_destination="New York",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
            estimated_cost=Decimal(estimated_cost) if estimated_cost else None,
            currency=currency,
            status=status,
        )
    )
    if approval is not None and status in (TripStatus.approved, TripStatus.completed):
        _trip_approval(s, trip, status=approval)
    return trip


def _flight_seg(
    s: FakeSession,
    trip: Trip,
    *,
    supplier: str = "United Airlines",
    amount: str = "450.00",
    cabin: str = "economy",
    hours: float = 3.0,
    in_policy: bool | None = True,
    currency: str = "USD",
) -> TripSegment:
    return s.add_obj(
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,
            segment_type=SegmentType.flight,
            supplier=supplier,
            start_datetime=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
            amount=Decimal(amount),
            currency=currency,
            in_policy=in_policy,
            details={"cabin_class": cabin, "duration_hours": hours},
        )
    )


def _hotel_seg(
    s: FakeSession, trip: Trip, *, location: str, nights: int, amount: str
) -> TripSegment:
    return s.add_obj(
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,
            segment_type=SegmentType.hotel,
            supplier="Marriott International",
            start_datetime=datetime(2026, 11, 1, 15, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 11, 1 + nights, 11, tzinfo=timezone.utc),
            location=location,
            amount=Decimal(amount),
            currency="USD",
            details={"nights": nights},
        )
    )


def _card(
    s: FakeSession, expense: Any, *, merchant: str, amount: str, user: uuid.UUID = EMPLOYEE
) -> CardTransaction:
    return s.add_obj(
        CardTransaction(
            id=uuid.uuid4(),
            organization_id=ORG,
            user_id=user,
            expense_id=expense.id if expense is not None else None,
            merchant=merchant,
            amount=Decimal(amount),
            currency="USD",
            txn_date=date(2026, 11, 1),
        )
    )


# ---------------------------------------------------------------------------
# R1 — claimant-typed / contradicted trip segments
# ---------------------------------------------------------------------------


class TestR1SegmentTrust:
    def test_draft_trip_segment_supplier_does_not_override_card_merchant(self) -> None:
        """Reviewer probe 1: SKYBRIDGE AIR 450 (card + deterministic OCR) linked to a
        DRAFT-trip segment typed as 'United Airlines' → must escalate on AIR-PREF."""
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.draft))
        r = _report(s, total="450.00")
        e = _entry(
            s,
            r,
            category="airfare",
            amount="450.00",
            vendor="SKYBRIDGE AIR",
            trip_segment_id=seg.id,
        )
        _card(s, e, merchant="SKYBRIDGE AIR", amount="450.00")
        _receipt(
            s, e, fields={"vendor": "SKYBRIDGE AIR", "amount": 450.0}, provenance={"vendor": DET}
        )

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF"), a.flags_snapshot
        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_draft_trip_hotel_nights_do_not_deflate_rate(self) -> None:
        """Reviewer probe 2: 480 one-night Chicago stay (deterministic receipt) linked
        to a DRAFT-trip 'New York, 4 nights' segment → must not read as 120/night."""
        s = _session_with_users()
        seg = _hotel_seg(
            s, _trip(s, status=TripStatus.draft), location="New York, NY", nights=4, amount="480.00"
        )
        r = _report(s, total="480.00")
        e = _entry(
            s, r, category="lodging", amount="480.00", vendor="Marriott", trip_segment_id=seg.id
        )
        _receipt(
            s,
            e,
            fields={"vendor": "Marriott", "amount": 480.0, "location": "Chicago, IL", "nights": 1},
            provenance={"vendor": DET},
        )

        a, route = _assess(s, r)

        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["amount_usd"] == pytest.approx(480.0), a.flags_snapshot
        assert route == "escalate"

    @pytest.mark.parametrize(
        "status",
        [TripStatus.draft, TripStatus.pending_approval, TripStatus.rejected, TripStatus.cancelled],
    )
    def test_unapproved_trip_segment_is_ignored(self, status: TripStatus) -> None:
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=status))
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="United Airlines", trip_segment_id=seg.id
            ),
        )
        assert _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    @pytest.mark.parametrize("status", [TripStatus.approved, TripStatus.completed])
    def test_approved_trip_segment_is_used(self, status: TripStatus) -> None:
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=status))
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare", amount=320.0, vendor="United Airlines", trip_segment_id=seg.id
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_approved_segment_supplier_loses_to_conflicting_card_merchant(self) -> None:
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.approved))
        txn = _card(s, None, merchant="SKYBRIDGE AIR", amount="320.00")
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="SKYBRIDGE AIR",
                trip_segment_id=seg.id,
                source_card_txn_id=txn.id,
            ),
        )
        assert _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_approved_segment_consistent_with_card_merchant_clears_air_pref(self) -> None:
        s = _session_with_users()
        seg = _flight_seg(s, _trip(s, status=TripStatus.completed))
        txn = _card(s, None, merchant="UNITED AIRLINES", amount="320.00")
        flags = _presubmit(
            s,
            _draft_entry(
                category="airfare",
                amount=320.0,
                vendor="United Airlines",
                trip_segment_id=seg.id,
                source_card_txn_id=txn.id,
            ),
        )
        assert not _pv(flags, "AIR-PREF"), [f.reason for f in flags]

    def test_approved_hotel_segment_city_conflicting_with_receipt_uses_lowest_cap(self) -> None:
        s = _session_with_users()
        seg = _hotel_seg(
            s,
            _trip(s, status=TripStatus.completed),
            location="New York, NY",
            nights=4,
            amount="956.00",
        )
        rec = _receipt(
            s,
            None,
            fields={"vendor": "Marriott", "amount": 956.0, "location": "Chicago, IL"},
            provenance={"vendor": DET, "location": DET},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging",
                amount=956.0,
                vendor="Marriott",
                trip_segment_id=seg.id,
                primary_receipt_id=rec.id,
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["cap"] == 160, [f.reason for f in flags]
        # The (contradicted) segment city is only a CLAIM: within its Tier 1 cap, so
        # the flag is pending verification — still a policy_violation (escalates).
        assert cap[0].observed["claimed_city"] == "New York"
        assert cap[0].pending_verification == "city"
        assert cap[0].flag_type == "policy_violation"

    def test_approved_hotel_segment_nights_capped_by_receipt_nights(self) -> None:
        s = _session_with_users()
        seg = _hotel_seg(
            s,
            _trip(s, status=TripStatus.completed),
            location="New York, NY",
            nights=4,
            amount="956.00",
        )
        rec = _receipt(
            s,
            None,
            fields={"vendor": "Marriott", "amount": 480.0, "nights": 1},
            provenance={"vendor": DET},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging",
                amount=480.0,
                vendor="Marriott",
                trip_segment_id=seg.id,
                primary_receipt_id=rec.id,
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(480.0), [
            f.reason for f in flags
        ]


# ---------------------------------------------------------------------------
# R2 — receipt location / nights / per-night provenance
# ---------------------------------------------------------------------------


class TestR2ReceiptFieldProvenance:
    @pytest.mark.parametrize("loc_prov", ["user_edited", "ai_inferred", None])
    def test_unverified_location_does_not_select_tier1_cap(self, loc_prov: str | None) -> None:
        """Reviewer probe: 230 one-night stay, location corrected to New York → must
        stay on the lowest (Tier 3, 160) cap; the claimed city only makes the flag
        pending verification — it is never auto-approved."""
        s = _session_with_users()
        r = _report(s, total="230.00")
        e = _entry(s, r, category="lodging", amount="230.00", vendor="Hilton Chicago")
        prov: dict[str, str] = {"vendor": DET}
        if loc_prov:
            prov["location"] = loc_prov
        _receipt(
            s,
            e,
            fields={
                "vendor": "Hilton Chicago",
                "amount": 230.0,
                "location": "New York, NY",
                "nights": 1,
            },
            provenance=prov,
        )

        a, route = _assess(s, r)

        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["cap"] == 160, a.flags_snapshot
        assert cap[0]["pending_verification"] == "city"
        assert route == "escalate"

    def test_deterministic_location_selects_tier1_cap(self) -> None:
        """Positive control: an OCR-read New York location legitimately gets 300."""
        s = _session_with_users()
        r = _report(s, total="230.00")
        e = _entry(s, r, category="lodging", amount="230.00", vendor="Hilton Midtown")
        _receipt(
            s,
            e,
            fields={
                "vendor": "Hilton Midtown",
                "amount": 230.0,
                "location": "New York, NY",
                "nights": 1,
            },
            provenance={"vendor": DET, "location": DET, "amount": DET},
        )

        a, route = _assess(s, r)

        assert not _violations(a, "HOTEL-CAP"), a.flags_snapshot
        assert a.is_policy_compliant is True
        assert route == "auto_approve"

    def test_user_edited_nights_are_ignored(self) -> None:
        s = _session_with_users()
        rec = _receipt(
            s,
            None,
            fields={"vendor": "Hilton", "amount": 400.0, "nights": 4, "location": "Chicago, IL"},
            provenance={"vendor": DET, "location": DET, "nights": "user_edited"},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging", amount=400.0, vendor="Hilton", primary_receipt_id=rec.id
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(400.0)

    def test_per_night_rate_ignored_when_receipt_amount_user_edited(self) -> None:
        """OCR 956 / 239 x 4; claimant corrects the receipt amount AND the entry to
        1500 → the stale 239 must not be used (1500/4 = 375 > 300)."""
        s = _session_with_users()
        rec = _receipt(
            s,
            None,
            fields={
                "vendor": "Marriott",
                "amount": 1500.0,
                "per_night_rate": 239.0,
                "nights": 4,
                "location": "New York, NY",
            },
            provenance={"vendor": DET, "location": DET, "amount": "user_edited"},
        )
        flags = _presubmit(
            s,
            _draft_entry(
                category="lodging", amount=1500.0, vendor="Marriott", primary_receipt_id=rec.id
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["amount_usd"] == pytest.approx(375.0)


# ---------------------------------------------------------------------------
# R3 — trip approvals re-evaluate the declared segments
# ---------------------------------------------------------------------------


def _assess_trip(s: FakeSession, trip: Trip) -> tuple[RiskAssessment, str]:
    a = assess_risk(trip, ApprovalSubjectType.trip, context=_ctx(), session=s)  # type: ignore[arg-type]
    return a, decide_route(a)


class TestR3TripReEvaluation:
    def test_clean_trip_auto_approves(self) -> None:
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="400.00")
        _flight_seg(s, t, amount="350.00")

        a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is True, a.policy_rationale
        assert route == "auto_approve"

    def test_claimed_in_policy_first_class_nonpreferred_escalates(self) -> None:
        """POST /trips with a 3,000 first-class SkyBridge segment marked in_policy=true
        and estimated_cost=100 used to auto-approve."""
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="100.00")
        _flight_seg(s, t, supplier="SKYBRIDGE AIR", amount="3000.00", cabin="first", in_policy=True)

        a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is False
        assert a.blocking_flag_count >= 2
        assert a.subject_total == pytest.approx(3000.0)
        assert route == "escalate"

    def test_understated_estimated_cost_is_floored_at_segment_sum(self) -> None:
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="100.00")
        _flight_seg(s, t, amount="350.00")
        _flight_seg(s, t, amount="350.00")

        a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is True, a.policy_rationale
        assert a.subject_total == pytest.approx(700.0)
        assert route == "escalate"

    def test_non_usd_segment_escalates(self) -> None:
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="300.00")
        _flight_seg(s, t, amount="300.00", currency="EUR")

        a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_stored_in_policy_none_still_escalates(self) -> None:
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="400.00")
        _flight_seg(s, t, amount="350.00", in_policy=None)

        a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_trip_evaluation_error_escalates(self) -> None:
        from unittest.mock import patch

        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="400.00")
        _flight_seg(s, t, amount="350.00")

        with patch.object(PolicyEvalTool, "evaluate_many", side_effect=RuntimeError("down")):
            a, route = _assess_trip(s, t)

        assert a.is_policy_compliant is False
        assert route == "escalate"


# ---------------------------------------------------------------------------
# R4 — stored requires_approver advisory flag
# ---------------------------------------------------------------------------


class TestR4StoredRequiresApprover:
    def test_stored_advisory_requires_approver_escalates_clean_report(self) -> None:
        s = _session_with_users()
        stored = {
            "flags": [
                {
                    "entry_ref": "Team dinner",
                    "flag_type": "approval_required",
                    "rule_id": str(uuid.uuid4()),
                    "severity": "advisory",
                    "requires_approver": "manager",
                    "reason": "Group meal requires manager approval.",
                }
            ],
            "blocking_count": 0,
            "advisory_count": 1,
        }
        r = _report(s, total="20.00", pre_submit_flags=stored)
        _entry(s, r, category="meals", amount="20.00", vendor="Cafe")

        a, route = _assess(s, r)

        assert a.is_policy_compliant is True  # advisory only…
        assert a.requires_approver is True  # …but the stored approver demand survives
        assert route == "escalate"


# ---------------------------------------------------------------------------
# R5 — previously untested bypass closures
# ---------------------------------------------------------------------------


class TestR5EvidenceOwnership:
    def test_stale_per_night_rate_not_used_for_edited_entry_amount(self) -> None:
        """Mutant A: receipt OCR 956 / 239 / 4 nights NYC; entry amount edited to 1500."""
        s = _session_with_users()
        r = _report(s, total="1500.00")
        e = _entry(s, r, category="lodging", amount="1500.00", vendor="Marriott")
        _receipt(
            s,
            e,
            fields={
                "vendor": "Marriott",
                "amount": 956.0,
                "per_night_rate": 239.0,
                "nights": 4,
                "location": "New York, NY",
            },
            provenance={"vendor": DET, "location": DET, "amount": DET},
        )

        a, _ = _assess(s, r)

        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["amount_usd"] == pytest.approx(375.0), a.flags_snapshot

    def test_other_users_deterministic_receipt_does_not_clear_air_pref(self) -> None:
        """Mutant B: receipt uploaded_by another employee."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _receipt(
            s,
            e,
            fields={"vendor": "United Airlines", "amount": 300.0},
            provenance={"vendor": DET},
            uploaded_by=OTHER_EMPLOYEE,
        )

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_other_users_card_txn_does_not_clear_air_pref(self) -> None:
        """Mutant C: card transaction belongs to another employee."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _card(s, e, merchant="United Airlines", amount="300.00", user=OTHER_EMPLOYEE)

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_owned_card_merchant_must_equal_vendor(self) -> None:
        """Mutant D: owned card txn from SkyBridge Air, entry vendor typed 'United'."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _card(s, e, merchant="SkyBridge Air", amount="300.00")

        a, route = _assess(s, r)

        assert _violations(a, "AIR-PREF")
        assert route == "escalate"

    def test_owned_matching_card_merchant_clears_air_pref(self) -> None:
        """Positive control for C/D: the owner's own United card txn is evidence."""
        s = _session_with_users()
        r = _report(s, total="300.00")
        e = _entry(s, r, category="airfare", amount="300.00", vendor="United Airlines")
        _card(s, e, merchant="United Airlines", amount="300.00")

        a, _ = _assess(s, r)

        # (route escalates on RECEIPT-REQ — airfare always needs a receipt)
        assert not _violations(a, "AIR-PREF"), a.flags_snapshot
