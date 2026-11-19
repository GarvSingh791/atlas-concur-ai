"""Follow-up (grades): AIR-07 / LOD-05 / NR-01 evaluators (hermetic, pure).

Detection from trusted vs typed words, every grade boundary, unknown grade, FX, the
policy-page validation / description and the engine-reach guarantee.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ai_agents.agents.policy_rule_semantics import describe_rule, validate_rule_definition
from ai_agents.tools import grade_entitlements as ge
from ai_agents.tools import process_rules as pr
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from config.constants import grades as grade_ladder
from database.seeds.grade_rules_content import (
    AIRPORT_EXTRAS_DEFINITION,
    GRADE_RULES,
    LOUNGE_MEMBERSHIP_DEFINITION,
    ROOM_UPGRADE_DEFINITION,
)

AIR07, LOD05, NR01 = (
    AIRPORT_EXTRAS_DEFINITION,
    ROOM_UPGRADE_DEFINITION,
    LOUNGE_MEMBERSHIP_DEFINITION,
)


def _detect(entitlement: str, *, category: str = "other", typed=(), trusted=(), items=None):
    return ge.detect(entitlement, category=category, typed=typed, trusted=trusted, line_items=items)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "DELTA SKY CLUB ATL",
            "United Club day pass",
            "Admirals Club",
            "Centurion Lounge",
            "Priority Pass visit",
            "Airport lounge access",
            "lounge day pass",
            "Southwest EarlyBird Check-In",
            "Priority boarding",
            "early boarding fee",
        ],
    )
    def test_air07_trusted(self, text: str) -> None:
        d = _detect(ge.AIRPORT_EXTRAS, trusted=(text,))
        assert d is not None and d.trusted is True

    @pytest.mark.parametrize(
        "text",
        ["Sky Lounge Bar & Grill", "Hotel lobby lounge drinks", "Uber to airport", "Checked bag"],
    )
    def test_air07_not_detected_off_airfare(self, text: str) -> None:
        # bare "lounge" is an airport lounge only on an airfare line
        assert _detect(ge.AIRPORT_EXTRAS, category="meals", trusted=(text,)) is None

    def test_bare_lounge_on_airfare_line(self) -> None:
        assert _detect(ge.AIRPORT_EXTRAS, category="airfare", trusted=("Lounge",)) is not None

    def test_membership_is_nr01_not_air07(self) -> None:
        text = "Admirals Club annual membership"
        assert _detect(ge.AIRPORT_EXTRAS, trusted=(text,)) is None
        d = _detect(ge.LOUNGE_MEMBERSHIP, trusted=(text,))
        assert d is not None and "membership" in d.matched

    @pytest.mark.parametrize("text", ["Priority Pass membership renewal", "United Club dues"])
    def test_nr01(self, text: str) -> None:
        assert _detect(ge.LOUNGE_MEMBERSHIP, trusted=(text,)) is not None

    def test_nr01_needs_a_lounge(self) -> None:
        assert _detect(ge.LOUNGE_MEMBERSHIP, trusted=("Gym membership",)) is None

    @pytest.mark.parametrize(
        "text", ["Junior Suite", "Executive suite, 2 nights", "Room upgrade", "upgrade to suite"]
    )
    def test_lod05_on_lodging(self, text: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="lodging", trusted=(text,)) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "Embassy Suites Chicago",  # brand (plural)
            "Homewood Suites by Hilton",
            "Marriott, 100 Main St Suite 400",  # address
            "Hilton Ste #12",
            "Standard king room",
        ],
    )
    def test_lod05_brand_and_address_never_match(self, text: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="lodging", trusted=(text,)) is None

    def test_suite_off_hotel_is_not_a_room(self) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="other", trusted=("Office suite licence",)) is None

    def test_plain_upgrade_only_on_lodging(self) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="lodging", trusted=("Upgrade",)) is not None
        assert _detect(ge.ROOM_UPGRADE, category="airfare", trusted=("Upgrade",)) is None

    def test_typed_words_widen_but_are_marked_untrusted(self) -> None:
        d = _detect(ge.AIRPORT_EXTRAS, typed=("lounge pass",), trusted=("Delta Air Lines",))
        assert d is not None and d.trusted is False

    def test_typed_words_never_remove_a_trusted_match(self) -> None:
        # the claimant calls it a "standard room"; the receipt says suite
        d = _detect(
            ge.ROOM_UPGRADE,
            category="lodging",
            typed=("Standard room",),
            trusted=("Hilton",),
            items=[("Junior Suite", 420.0)],
        )
        assert d is not None and d.trusted is True

    def test_line_item_amount_is_the_part_at_stake(self) -> None:
        d = _detect(
            ge.ROOM_UPGRADE,
            category="lodging",
            items=[("Room 2 nights", 400.0), ("Suite upgrade", 120.0), ("Upgrade fee", 30.0)],
        )
        assert d is not None and d.amount == Decimal("150.00")

    def test_mileage_never(self) -> None:
        assert _detect(ge.AIRPORT_EXTRAS, category="mileage", typed=("lounge pass",)) is None

    def test_nothing_named(self) -> None:
        assert _detect(ge.AIRPORT_EXTRAS, typed=("Lunch",), trusted=("Chipotle",)) is None

    # -- review fix: the claimant's CATEGORY never narrows trusted evidence ------------

    @pytest.mark.parametrize("category", ["other", "meals", "lodging"])
    def test_folio_upgrade_line_detected_whatever_the_category(self, category: str) -> None:
        # The hotel context comes from the WHOLE receipt (vendor + other line items).
        d = _detect(
            ge.ROOM_UPGRADE,
            category=category,
            trusted=("Hyatt Regency",),
            items=[("Room Charge", 250.0), ("Upgrade", 80.0)],
        )
        assert d is not None and d.trusted is True and d.amount == Decimal("80.00")

    @pytest.mark.parametrize("category", ["other", "meals"])
    def test_junior_suite_supplement_on_a_folio_filed_as_other(self, category: str) -> None:
        d = _detect(
            ge.ROOM_UPGRADE,
            category=category,
            trusted=("Marriott Marquis",),
            items=[("Room Charge", 250.0), ("Junior Suite Supplement", 120.0)],
        )
        assert d is not None and d.amount == Decimal("120.00")

    def test_hotel_brand_vendor_alone_places_a_suite_line_at_a_hotel(self) -> None:
        d = _detect(
            ge.ROOM_UPGRADE, category="other", trusted=("Hyatt Regency",), items=[("Upgrade", 80.0)]
        )
        assert d is not None

    @pytest.mark.parametrize("category", ["other", "meals"])
    def test_airline_lounge_line_detected_whatever_the_category(self, category: str) -> None:
        d = _detect(
            ge.AIRPORT_EXTRAS,
            category=category,
            trusted=("United Airlines",),
            items=[("Lounge", 59.0)],
        )
        assert d is not None and d.amount == Decimal("59.00")

    @pytest.mark.parametrize(
        ("entitlement", "segment", "line"),
        [(ge.ROOM_UPGRADE, "hotel", "Upgrade"), (ge.AIRPORT_EXTRAS, "flight", "Lounge")],
    )
    def test_linked_segment_adds_context(self, entitlement: str, segment: str, line: str) -> None:
        assert _detect(entitlement, items=[(line, 50.0)]) is None
        d = ge.detect(
            entitlement,
            category="other",
            typed=(),
            trusted=(),
            line_items=[(line, 50.0)],
            segment_type=segment,
        )
        assert d is not None

    def test_airport_word_alone_is_not_airline_context(self) -> None:
        # "Airport Marriott" lobby lounge is a hotel bar, not an airport lounge.
        assert (
            _detect(ge.AIRPORT_EXTRAS, trusted=("Airport Marriott",), items=[("Lounge", 18.0)])
            is None
        )

    def test_software_suite_still_not_a_room(self) -> None:
        assert (
            _detect(ge.ROOM_UPGRADE, trusted=("Adobe Inc",), items=[("Creative Suite", 60.0)])
            is None
        )

    # -- review fix: a "Suite <n>" room line is not an address --------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "Executive Suite 2 nights",
            "Junior Suite 11/12-11/14",
            "Suite 1 night",
            "Suite 3 nts",
            "Suite 2x",
            "Deluxe Suite",
        ],
    )
    def test_suite_room_lines_detected(self, text: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="lodging", trusted=(text,)) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "Suite 400",
            "100 Main St, Suite 400, Chicago",
            "Suite #12",
            "Ste #12",
            "Suite No. 4",
            "Embassy Suites",
        ],
    )
    def test_suite_addresses_and_brands_not_detected(self, text: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="lodging", trusted=(text,)) is None

    # -- review fix: software "suite upgrade" / conference "room" are not a hotel -------

    @pytest.mark.parametrize(
        "text",
        [
            "Adobe Creative Suite upgrade",
            "Microsoft Office Suite upgrade",
            "Upgrade to Executive plan",
            "Upgrade to Deluxe edition",
            "Upgrade to a Suite licence",
        ],
    )
    @pytest.mark.parametrize("category", ["other", "meals", "entertainment"])
    def test_software_suite_upgrade_is_not_a_room(self, text: str, category: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category=category, trusted=(text,)) is None
        assert _detect(ge.ROOM_UPGRADE, category=category, items=[(text, 99.0)]) is None
        assert _detect(ge.ROOM_UPGRADE, category=category, typed=(text,)) is None

    @pytest.mark.parametrize(
        ("trusted", "item"),
        [
            ("Regus - Conference room", "AV upgrade"),
            ("Staples - Room divider", "Upgrade warranty"),
            ("WeWork meeting rooms", "Upgrade"),
        ],
    )
    def test_conference_room_receipt_is_not_a_hotel(self, trusted: str, item: str) -> None:
        assert (
            _detect(
                ge.ROOM_UPGRADE,
                category="other",
                trusted=(trusted,),
                items=[(item, 10.0)],
            )
            is None
        )

    @pytest.mark.parametrize(
        ("text", "matched"),
        [
            ("Suite upgrade", "Suite upgrade"),
            ("Upgrade to suite", "Upgrade to suite"),
            ("upgrade to a club room", "upgrade to a club"),
            ("Upgrade", "Upgrade"),
        ],
    )
    def test_suite_upgrade_still_detected_at_a_hotel(self, text: str, matched: str) -> None:
        # Same words, hotel context from the folio (brand vendor / room-night line).
        d = _detect(
            ge.ROOM_UPGRADE,
            category="other",
            trusted=("Hyatt Regency",),
            items=[(text, 80.0)],
        )
        assert d is not None and d.amount == Decimal("80.00")
        assert d.matched == matched  # the reason names the whole phrase
        d = _detect(
            ge.ROOM_UPGRADE,
            category="other",
            trusted=("Acme Stays",),
            items=[("Room night", 200.0), (text, 80.0)],
        )
        assert d is not None and d.amount == Decimal("80.00")

    @pytest.mark.parametrize("text", ["Room upgrade", "Hotel upgrade", "Upgraded room"])
    def test_hotel_shaped_upgrade_needs_no_context(self, text: str) -> None:
        assert _detect(ge.ROOM_UPGRADE, category="other", trusted=(text,)) is not None


# ---------------------------------------------------------------------------
# Evaluation by grade
# ---------------------------------------------------------------------------

LOUNGE = ge.Detection(ge.AIRPORT_EXTRAS, "Sky Club", trusted=True)


def _eval(defn, grade, *, detection=LOUNGE, amount=59, currency="USD"):
    return ge.evaluate(
        defn, detection=detection, traveller_grade=grade, claim_amount=amount, currency=currency
    )


class TestByGrade:
    @pytest.mark.parametrize("grade", ["CSUITE", "SVP"])
    def test_entitled_is_in_policy_with_a_note(self, grade: str) -> None:
        f = _eval(AIR07, grade)
        assert f.flag_type == "policy_advisory" and f.severity == "advisory"
        assert f.requires_approver is None
        assert f.clause_ref == "AIR-07" and "AIR-07" in f.reason
        assert grade_ladder.title_of(grade) in f.reason

    @pytest.mark.parametrize("grade", ["VP", "DIRECTOR", "MANAGER", "SENIOR_IC", "IC", "INTERN"])
    def test_junior_is_a_violation_for_an_svp_approver(self, grade: str) -> None:
        f = _eval(AIR07, grade)
        assert f.flag_type == "policy_violation" and f.severity == "blocking"
        assert f.requires_approver == "SVP"
        assert f.observed["required_grade"] == "SVP"
        assert f.observed["justifiable_exception"] is True
        assert "4 hours" in f.reason  # AIR-07's delay exception is named

    @pytest.mark.parametrize("grade", [None, "", "svp", "Senior Vice President", "BOSS"])
    def test_unknown_grade_fails_closed(self, grade) -> None:
        f = _eval(LOD05, grade)
        assert f.flag_type == "approval_required" and f.requires_approver == "SVP"
        assert f.observed["could_not_verify"] == "traveller_grade"
        assert f.observed["traveller_grade"] is None

    def test_vp_svp_boundary_follows_the_rule_threshold(self) -> None:
        assert _eval(NR01, "VP").flag_type == "policy_violation"
        assert _eval({**NR01, "min_grade": "VP"}, "VP").flag_type == "policy_advisory"
        assert _eval({**NR01, "min_grade": "CSUITE"}, "SVP").requires_approver == "CSUITE"

    def test_threshold_off_the_ladder_raises(self) -> None:
        with pytest.raises(ValueError):
            _eval({**NR01, "min_grade": "Boss"}, "CSUITE")

    def test_amount_converted_to_usd(self) -> None:
        f = _eval(AIR07, "IC", amount=50, currency="EUR")
        assert f.observed["currency"] == "EUR"
        usd = Decimal(f.observed["amount_usd"])
        assert usd != Decimal("50.00") and usd > 0
        assert "≈ $" in f.reason

    def test_unknown_currency_is_not_treated_as_usd(self) -> None:
        f = _eval(AIR07, "SVP", amount=50, currency="XYZ")
        assert f.observed["amount_usd"] is None
        assert f.observed["could_not_verify"] == "currency"

    def test_line_item_amount_used_over_the_claim(self) -> None:
        det = ge.Detection(ge.ROOM_UPGRADE, "Suite upgrade", trusted=True, amount=Decimal("120"))
        f = _eval(LOD05, "IC", detection=det, amount=520)
        assert f.observed["amount"] == "120" and "$120.00" in f.reason

    def test_typed_only_detection_is_said_out_loud(self) -> None:
        det = ge.Detection(ge.AIRPORT_EXTRAS, "lounge pass", trusted=False)
        f = _eval(AIR07, "IC", detection=det)
        assert "not on a receipt or card" in f.reason
        assert f.observed["evidence"] == "entry_text"


# ---------------------------------------------------------------------------
# Rules as data: engine reach, validation, description
# ---------------------------------------------------------------------------


class TestRulesAsData:
    def test_seeded_rules(self) -> None:
        assert [r["clause_ref"] for r in GRADE_RULES] == ["AIR-07", "LOD-05", "NR-01"]
        for r in GRADE_RULES:
            assert pr.process_kind(r["definition"]) == pr.KIND_GRADE_ENTITLEMENT
            assert (r["rule_type"], r["applies_to"]) == pr.PROCESS_KIND_SHAPES[
                pr.KIND_GRADE_ENTITLEMENT
            ]

    @pytest.mark.parametrize("rule", GRADE_RULES, ids=lambda r: r["clause_ref"])
    def test_engine_never_evaluates_them_per_line(self, rule: dict) -> None:
        # eval-db invariant: PolicyEvalTool verdicts are unchanged by these rules.
        for item in (
            EvalItem(item_type="receipt", amount_usd=59.0, expense_category="airfare"),
            EvalItem(item_type="hotel", amount_usd=400.0, city="Denver"),
        ):
            assert not PolicyEvalTool.rule_applies(
                rule["rule_type"], rule["applies_to"], rule["definition"], item
            )

    @pytest.mark.parametrize("rule", GRADE_RULES, ids=lambda r: r["clause_ref"])
    def test_valid_and_described(self, rule: dict) -> None:
        assert (
            validate_rule_definition(rule["rule_type"], rule["applies_to"], rule["definition"])
            == ()
        )
        text = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
        assert rule["clause_ref"] in text and "SVP" in text and "Warning" not in text

    @pytest.mark.parametrize(
        "bad",
        [
            {**AIR07, "entitlement": "spa"},
            {**AIR07, "min_grade": "Boss"},
            {**AIR07, "delay_exception_hours": 0},
            {**LOD05, "delay_exception_hours": 4},
            {**NR01, "unexpected": 1},
        ],
    )
    def test_invalid_shapes_refused(self, bad: dict) -> None:
        assert validate_rule_definition("category_allow", "all", bad) != ()
