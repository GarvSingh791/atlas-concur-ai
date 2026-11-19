"""Issue 6 — nightly / per-diem math from claimed vs verified stay length and city.

Hermetic: the FakeSession + seeded-rules harness of ``test_c7_05_15_policy_gates``
(real ``policy_content.RULES`` — the canonical TEP-FY26 figures), so the chain
Expense -> DraftEntry -> run_presubmit -> PolicyEvalTool -> flags -> RiskAssessment
-> decide_route runs as in production.

Governance pinned here:
  * a claimed stay length / city NEVER clears a rule: the line stays a
    policy_violation, so approval-time evaluation escalates it (never auto-approved);
  * the claimant may submit it (advisory, ``pending_verification``) only when the
    line is within a cap at the claimed figures AND the rule is justifiable at
    manager level;
  * verified evidence always beats a claim.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from ai_agents.agents.approval_agent.evaluate import _is_blocking
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    DAYS_RE,
    NIGHTS_RE,
    claimed_count,
    verdicts_to_flags,
)
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    EMPLOYEE,
    ORG,
    FakeSession,
    _assess,
    _entry,
    _presubmit,
    _pv,
    _receipt,
    _report,
    _seed_rules,
    _seeded_rules,
    _session_with_users,
    _trip_approval,
    _violations,
)
from ai_agents.tools.policy_eval import PolicyEvalTool, RuleVerdict
from database.models.approval import ApprovalStatus
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

DET = "deterministic"


def _described(entry: Any, description: str) -> Any:
    entry.description = description
    return entry


def _draft(
    *,
    category: str,
    amount: float,
    vendor: str | None = None,
    description: str | None = None,
    trip_segment_id: uuid.UUID | None = None,
    primary_receipt_id: uuid.UUID | None = None,
    entry_id: str | None = None,
) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-11-02",
        vendor=vendor,
        description=description,
        status="draft",
        match_source=None,
        trip_segment_id=str(trip_segment_id) if trip_segment_id else None,
        is_itemized=False,
        primary_receipt_id=str(primary_receipt_id) if primary_receipt_id else None,
        source_card_txn_id=None,
        sibling_group_id=None,
        entry_id=entry_id,
    )


def _trip(s: FakeSession, *, status: TripStatus, approval: ApprovalStatus | None) -> Trip:
    trip = s.add_obj(
        Trip(
            id=uuid.uuid4(),
            organization_id=ORG,
            traveler_id=EMPLOYEE,
            origin="SFO",
            primary_destination="New York",
            start_date=datetime(2026, 11, 1).date(),
            end_date=datetime(2026, 11, 4).date(),
            currency="USD",
            status=status,
        )
    )
    if approval is not None:
        _trip_approval(s, trip, status=approval)
    return trip


def _segment(
    s: FakeSession,
    trip: Trip,
    *,
    seg_type: SegmentType = SegmentType.hotel,
    location: str = "New York, NY",
    nights: int = 3,
) -> TripSegment:
    return s.add_obj(
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,
            segment_type=seg_type,
            supplier="Marriott International",
            start_datetime=datetime(2026, 11, 1, 15, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 11, 1 + nights, 11, tzinfo=timezone.utc),
            location=location,
            amount=Decimal("750.00"),
            currency="USD",
            details={"nights": nights},
        )
    )


# ---------------------------------------------------------------------------
# Description parsing
# ---------------------------------------------------------------------------


class TestClaimedCountParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hotel - 3 nights @ 250.00/night", 3),
            ("Hotel — 3 nights @ 289.00/night (cap 250.00)", 3),
            ("3-night stay", 3),
            ("Two nights at the Hilton", 2),
            ("1 night", 1),
            ("Hotel 289.00/night", None),  # a rate, not a stay length
            ("2 nights + 1 night", None),  # ambiguous
            ("99 nights", None),  # out of range
            ("0 nights", None),
            (None, None),
        ],
    )
    def test_nights(self, text: str | None, expected: int | None) -> None:
        assert claimed_count(text, NIGHTS_RE, 60) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("Conference meals (3 days)", 3), ("3-day offsite", 3), ("per day 65", None)],
    )
    def test_days(self, text: str, expected: int | None) -> None:
        assert claimed_count(text, DAYS_RE, 31) == expected


# ---------------------------------------------------------------------------
# Hotel nights
# ---------------------------------------------------------------------------


class TestHotelNights:
    def _nyc_receipt_report(self, s: FakeSession, amount: str, description: str) -> Any:
        r = _report(s, total=amount)
        e = _described(
            _entry(s, r, category="lodging", amount=amount, vendor="Marriott Marquis"),
            description,
        )
        _receipt(
            s,
            e,
            fields={
                "vendor": "Marriott Marquis",
                "amount": float(amount),
                "location": "New York, NY",
            },
            provenance={"vendor": DET, "amount": DET, "location": DET},
        )
        return r

    def test_description_nights_within_cap_is_advisory_pending_and_escalates(self) -> None:
        s = _session_with_users()
        r = self._nyc_receipt_report(s, "750.00", "Hotel - 3 nights @ 250.00/night")
        a, route = _assess(s, r)
        cap = _violations(a, "HOTEL-CAP")
        assert cap, a.flags_snapshot
        flag = cap[0]
        assert flag["severity"] == "advisory"
        assert flag["pending_verification"] == "nights"
        assert flag["observed"]["cap"] == 300.0
        assert flag["observed"]["claimed_nightly_usd"] == 250.0
        assert "nights could not be verified" in flag["reason"]
        # Never auto-approved: a pending-verification flag is still an exception.
        assert _is_blocking(flag)
        assert a.is_policy_compliant is False
        assert route == "escalate"

    def test_description_nights_still_over_cap_is_blocking(self) -> None:
        s = _session_with_users()
        r = self._nyc_receipt_report(s, "990.00", "Hotel 3 nights")
        a, route = _assess(s, r)
        cap = _violations(a, "HOTEL-CAP")
        assert cap[0]["severity"] == "blocking"
        assert cap[0]["pending_verification"] is None
        assert "330.00 USD per night, which exceeds the Tier 1 cap 300.00" in cap[0]["reason"]
        assert route == "escalate"

    def test_verified_receipt_nights_beat_claimed_description(self) -> None:
        s = _session_with_users()
        r = _report(s, total="450.00")
        e = _described(
            _entry(s, r, category="lodging", amount="450.00", vendor="Hilton"), "5 nights"
        )
        _receipt(
            s,
            e,
            fields={"vendor": "Hilton", "amount": 450.0, "location": "New York, NY", "nights": 1},
            provenance={"vendor": DET, "amount": DET, "location": DET},
        )
        a, route = _assess(s, r)
        cap = _violations(a, "HOTEL-CAP")
        assert cap[0]["observed"]["amount_usd"] == pytest.approx(450.0)
        assert "claimed_nights" not in cap[0]["observed"]
        assert cap[0]["severity"] == "blocking"
        assert route == "escalate"

    def test_untrusted_segment_nights_and_city_are_claims(self) -> None:
        """A DRAFT trip's hotel (3 nights, New York): 750 -> 250/night is within the
        Tier 1 cap only if both are verified -> pending, escalates."""
        s = _session_with_users()
        seg = _segment(s, _trip(s, status=TripStatus.draft, approval=None), nights=3)
        flags = _presubmit(
            s, _draft(category="lodging", amount=750.0, vendor="Hilton", trip_segment_id=seg.id)
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert cap and cap[0].observed["cap"] == 160.0
        assert cap[0].observed["claimed_nights"] == 3
        assert cap[0].observed["claimed_city"] == "New York"
        assert cap[0].pending_verification == "nights_and_city"
        assert cap[0].severity == "advisory"

    def test_other_travelers_segment_gives_no_claims(self) -> None:
        s = _session_with_users()
        trip = _trip(s, status=TripStatus.draft, approval=None)
        trip.traveler_id = uuid.uuid4()
        seg = _segment(s, trip)
        flags = _presubmit(
            s, _draft(category="lodging", amount=750.0, vendor="Hilton", trip_segment_id=seg.id)
        )
        cap = _pv(flags, "HOTEL-CAP")
        assert "claimed_nights" not in cap[0].observed
        assert cap[0].severity == "blocking"

    def test_q3_browser_path_without_city_is_tier3_and_says_so(self) -> None:
        """No receipt, no trip, no city anywhere: Tier 3 160 (not a fourth cap)."""
        s = _session_with_users()
        flags = _presubmit(
            s,
            _draft(category="lodging", amount=750.0, description="Hotel - 3 nights @ 250.00/night"),
        )
        cap = _pv(flags, "HOTEL-CAP")[0]
        assert cap.observed["cap"] == 160.0
        assert cap.severity == "blocking"
        assert "lowest cap (Tier 3, 160.00 USD per night)" in cap.reason
        assert "250.00 USD per night" in cap.reason

    def test_q3_browser_path_with_nyc_named_shows_the_300_cap(self) -> None:
        s = _session_with_users()
        flags = _presubmit(
            s,
            _draft(
                category="lodging",
                amount=750.0,
                vendor="Marriott Marquis New York",
                description="Hotel - 3 nights @ 250.00/night",
            ),
        )
        cap = _pv(flags, "HOTEL-CAP")[0]
        assert "within the Tier 1 New York cap of $300/night if the city is confirmed" in cap.reason
        assert cap.pending_verification == "nights_and_city"
        assert cap.severity == "advisory"
        assert "$180" not in cap.reason and "250.00 USD cap" not in cap.reason

    def test_ambiguous_nights_text_is_ignored(self) -> None:
        s = _session_with_users()
        flags = _presubmit(
            s,
            _draft(category="lodging", amount=450.0, description="2 nights + 1 night extension"),
        )
        cap = _pv(flags, "HOTEL-CAP")[0]
        assert "claimed_nights" not in cap.observed
        assert "Number of nights could not be verified" in cap.reason
        assert 'Add the number of nights to the description (e.g. "3 nights")' in cap.reason

    def test_nights_unknown_under_cap_emits_no_flag(self) -> None:
        """The old C1 'nights unknown' not_evaluated advisory is gone: the full amount
        is within the nightly cap, so any stay length is within it."""
        s = _session_with_users()
        flags = _presubmit(s, _draft(category="lodging", amount=150.0, vendor="Motel"))
        assert not _pv(flags, "HOTEL-CAP")
        assert not [f for f in flags if f.flag_type == "not_evaluated"]

    def test_pending_never_softens_a_non_justifiable_rule(self) -> None:
        """A HOTEL-CAP authored with an above-manager approver stays blocking even when
        within the cap at the claimed nights."""
        rules = _seed_rules()
        for rule in rules:
            if rule.source_clause_ref == "HOTEL-CAP":
                rule.definition = {**rule.definition, "exception_approver": "VP"}
        s = _session_with_users()
        with patch.object(PolicyEvalTool, "_load_active_rules", return_value=rules):
            flags = _presubmit(
                s,
                _draft(
                    category="lodging",
                    amount=450.0,
                    vendor="Hilton Chicago",
                    description="3 nights",
                ),
            )
        cap = _pv(flags, "HOTEL-CAP")[0]
        assert cap.observed["within_cap_if_verified"] is True
        assert cap.justifiable is False
        assert cap.pending_verification is None
        assert cap.severity == "blocking"


def test_verdicts_to_flags_gates_pending_on_justifiability() -> None:
    observed = {"within_cap_if_verified": True, "unverified": ["nights"]}

    def verdict(approver: str | None, rule_type: str = "amount_cap") -> RuleVerdict:
        return RuleVerdict(
            rule_id="r",
            rule_type=rule_type,
            applies_to="hotel",
            source_clause_ref="HOTEL-CAP",
            in_policy=False,
            reason="x",
            requires_approver=approver,
            observed=observed,
        )

    (ok,), _ = verdicts_to_flags(
        [verdict("manager")], entry_ref="e", has_receipt=True, category="lodging"
    )
    assert ok.severity == "advisory" and ok.pending_verification == "nights"
    (vp,), _ = verdicts_to_flags(
        [verdict("VP")], entry_ref="e", has_receipt=True, category="lodging"
    )
    assert vp.severity == "blocking" and vp.pending_verification is None
    (cat,), _ = verdicts_to_flags(
        [verdict(None, "category_allow")], entry_ref="e", has_receipt=True, category="lodging"
    )
    assert cat.severity == "blocking" and cat.pending_verification is None


# ---------------------------------------------------------------------------
# Meals days / city
# ---------------------------------------------------------------------------


class _MealSumSession(FakeSession):
    """FakeSession that also answers the per-segment meals total lookup."""

    def __init__(self, persisted_other_meals: str = "0") -> None:
        super().__init__()
        self.persisted_other_meals = Decimal(persisted_other_meals)

    def scalar(self, stmt: Any) -> Any:
        if "sum(expenses.amount)" in str(stmt):
            return self.persisted_other_meals
        return super().scalar(stmt)

    def scalars(self, stmt: Any) -> Any:
        """Answer the trip-wide meal-scope lookup (all segments of the trip)."""
        sql = str(stmt)
        if "FROM trip_segments" in sql:
            params = stmt.compile().params
            trip_ids = {v for v in params.values() if isinstance(v, uuid.UUID)}
            return _SegRows(
                [
                    o
                    for (m, _), o in self.objects.items()
                    if m is TripSegment and o.trip_id in trip_ids
                ]
            )
        return super().scalars(stmt)


class _SegRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


def _meal_session(persisted: str = "0") -> _MealSumSession:
    base = _session_with_users()
    s = _MealSumSession(persisted)
    s.objects.update(base.objects)
    return s


class TestMeals:
    def test_meals_three_days_description_per_day_blocking(self) -> None:
        s = _session_with_users()
        flags = _presubmit(
            s, _draft(category="meals", amount=205.5, description="Conference meals (3 days)")
        )
        pd = _pv(flags, "MEALS-PD")[0]
        assert pd.observed["claimed_daily_usd"] == 68.5
        assert pd.observed["rate"] == 65.0
        assert pd.severity == "blocking"
        assert "60.00" not in pd.reason

    def test_meals_city_from_deterministic_receipt_selects_tier_and_days_pending(self) -> None:
        s = _session_with_users()
        r = _report(s, total="210.00")
        e = _described(
            _entry(s, r, category="meals", amount="210.00", vendor="Lou Malnati's"),
            "Team meals (3 days)",
        )
        _receipt(
            s,
            e,
            fields={"vendor": "Lou Malnati's", "amount": 210.0, "location": "Chicago, IL"},
            provenance={"vendor": DET, "amount": DET, "location": DET},
        )
        a, route = _assess(s, r)
        pd = _violations(a, "MEALS-PD")[0]
        assert pd["observed"]["tier"] == "tier2" and pd["observed"]["rate"] == 75.0
        assert pd["pending_verification"] == "days"
        assert pd["severity"] == "advisory"
        assert route == "escalate"

    @pytest.mark.parametrize("prov", ["user_edited", "ai_inferred"])
    def test_unverified_meal_location_is_only_a_claim(self, prov: str) -> None:
        s = _session_with_users()
        r = _report(s, total="80.00")
        e = _entry(s, r, category="meals", amount="80.00", vendor="Deli")
        _receipt(
            s,
            e,
            fields={"vendor": "Deli", "amount": 80.0, "location": "New York, NY"},
            provenance={"vendor": DET, "amount": DET, "location": prov},
        )
        a, route = _assess(s, r)
        pd = _violations(a, "MEALS-PD")[0]
        assert pd["observed"]["rate"] == 65.0
        assert pd["observed"]["claimed_rate"] == 85.0
        assert pd["pending_verification"] == "city"
        assert route == "escalate"

    def test_unknown_days_compares_daily_rate(self) -> None:
        s = _session_with_users()
        flags = _presubmit(s, _draft(category="meals", amount=70.0, vendor="Cafe"))
        pd = _pv(flags, "MEALS-PD")[0]
        assert pd.observed["amount_usd"] == 70.0 and pd.severity == "blocking"
        assert 'state the number of days in the description (e.g. "3 days")' in pd.reason

    def test_trusted_segment_days_cover_all_meals_lines_on_it(self) -> None:
        """Two 150 lines on one human-approved 2-night NYC hotel (3 days): the per-day
        allowance covers BOTH (300 / 3 = 100 > 85) — never 150 / 3 per line."""
        s = _meal_session()
        seg = _segment(
            s, _trip(s, status=TripStatus.completed, approval=ApprovalStatus.approved), nights=2
        )
        flags = _presubmit(
            s,
            _draft(category="meals", amount=150.0, vendor="A", trip_segment_id=seg.id),
            _draft(category="meals", amount=150.0, vendor="B", trip_segment_id=seg.id),
        )
        pd = _pv(flags, "MEALS-PD")
        assert len(pd) == 2
        for flag in pd:
            assert flag.observed["days"] == 3
            assert flag.observed["total_usd"] == 300.0
            assert flag.observed["amount_usd"] == pytest.approx(100.0)
            assert flag.observed["tier"] == "tier1"

    def test_overlapping_segments_of_one_trip_share_one_allowance(self) -> None:
        """Security review: meals split across two OVERLAPPING segments of one approved
        trip (a 3-day hotel + a flight on day 1) must not each get the full per-day
        allowance. The trip covers 3 calendar days -> 3 x 85 = 255 for ALL its meals:
        2 x 200 = 400 -> 133.33/day > 85, flagged on both lines."""
        s = _meal_session()
        trip = _trip(s, status=TripStatus.completed, approval=ApprovalStatus.approved)
        hotel = _segment(s, trip, nights=2)  # Nov 1 15:00 -> Nov 3 11:00 = 3 days
        flight = _segment(s, trip, seg_type=SegmentType.flight, nights=0)  # Nov 1 only
        flags = _presubmit(
            s,
            _draft(category="meals", amount=200.0, vendor="A", trip_segment_id=hotel.id),
            _draft(category="meals", amount=200.0, vendor="B", trip_segment_id=flight.id),
        )
        pd = _pv(flags, "MEALS-PD")
        assert len(pd) == 2
        for flag in pd:
            assert flag.observed["days"] == 3
            assert flag.observed["total_usd"] == 400.0
            assert flag.observed["amount_usd"] == pytest.approx(400.0 / 3)

    def test_trusted_segment_days_include_persisted_lines_elsewhere(self) -> None:
        s = _meal_session(persisted="200.00")
        seg = _segment(
            s, _trip(s, status=TripStatus.completed, approval=ApprovalStatus.approved), nights=2
        )
        flags = _presubmit(
            s, _draft(category="meals", amount=100.0, vendor="A", trip_segment_id=seg.id)
        )
        pd = _pv(flags, "MEALS-PD")[0]
        assert pd.observed["total_usd"] == 300.0

    def test_single_line_within_segment_allowance_is_in_policy(self) -> None:
        s = _meal_session()
        seg = _segment(
            s, _trip(s, status=TripStatus.completed, approval=ApprovalStatus.approved), nights=2
        )
        flags = _presubmit(
            s, _draft(category="meals", amount=240.0, vendor="A", trip_segment_id=seg.id)
        )
        assert not _pv(flags, "MEALS-PD")  # 240 / 3 days = 80 <= 85 (Tier 1)

    def test_segment_total_lookup_failure_fails_closed(self) -> None:
        """The plain FakeSession cannot answer the total lookup -> no per-day average:
        the line is compared with ONE day's rate."""
        s = _session_with_users()
        seg = _segment(
            s, _trip(s, status=TripStatus.completed, approval=ApprovalStatus.approved), nights=2
        )
        flags = _presubmit(
            s, _draft(category="meals", amount=240.0, vendor="A", trip_segment_id=seg.id)
        )
        pd = _pv(flags, "MEALS-PD")[0]
        assert "days" not in pd.observed
        assert pd.observed["amount_usd"] == 240.0
        assert pd.observed["city"] == "New York, NY"


def test_pending_flag_never_auto_approves_small_report() -> None:
    """A 120 meal claimed as 2 days with a verified Tier 2 city: within the per diem
    only at the claimed days -> pending, total < auto-approve threshold, still escalates."""
    s = _session_with_users()
    r = _report(s, total="120.00")
    e = _described(_entry(s, r, category="meals", amount="120.00", vendor="Bistro"), "2 days")
    _receipt(
        s,
        e,
        fields={"vendor": "Bistro", "amount": 120.0, "location": "Seattle, WA"},
        provenance={"vendor": DET, "amount": DET, "location": DET},
    )
    a, route = _assess(s, r)
    pd = _violations(a, "MEALS-PD")[0]
    assert pd["pending_verification"] == "days"
    assert a.blocking_flag_count >= 1
    assert route == "escalate"


def test_context_unit_rejects_invalid_counts() -> None:
    from backend.app.core.errors import UnprocessableEntityError
    from backend.app.modules.policy.router import _context_unit

    for bad in (0, -1, True, "abc"):
        with pytest.raises(UnprocessableEntityError):
            _context_unit({"item_type": "hotel", "amount_usd": 750, "claimed_nights": bad})
    unit = _context_unit({"item_type": "meals", "amount_usd": 210, "claimed_days": 3})
    assert unit["item"]["claimed_days"] == 3
