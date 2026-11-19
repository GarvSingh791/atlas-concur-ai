"""Known limits — AIR-04 decided from the traveller's grade of record (hermetic).

AIR-04: "SVPs and above may book business class on flights > 6 hours."  The ladder is
``config/constants/grades.py``; the grade is the OWNER's ``users.grade`` (DB-backed
wiring is covered in ``backend/tests/test_kl_grade_db.py``).

Covers every grade boundary (C-suite / SVP entitled, VP and below not, unknown =
could not verify), the hour boundary, first class, the AIR-CABIN verdict note, the
planner notices, the what-if refusal of a claimed grade, rule validation and the
narrative step keeping the notices.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai_agents.agents import process_checks
from ai_agents.agents import process_rule_semantics as sem
from ai_agents.agents.travel_agent.narrative import enrich_with_narratives
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.tools import process_rules as pr
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from config.constants import grades as G
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import RULES

CABIN: dict[str, Any] = next(r["definition"] for r in RULES if r["clause_ref"] == "AIR-CABIN")
ENTITLED = ("CSUITE", "SVP")
NOT_ENTITLED = ("VP", "DIRECTOR", "MANAGER", "SENIOR_IC", "IC", "INTERN")
UNKNOWN: tuple[str | None, ...] = (None, "", "svp", "BOGUS", "Senior Vice President")


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class TestLadder:
    def test_ladder_is_strictly_ordered_and_unique(self) -> None:
        ranks = [g.rank for g in G.GRADES]
        assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == len(ranks)
        assert len(set(G.GRADE_CODES)) == len(G.GRADE_CODES)
        assert G.SENIOR_EXCEPTION_MIN_GRADE in G.GRADE_CODES

    @pytest.mark.parametrize("code", ENTITLED)
    def test_svp_and_above(self, code: str) -> None:
        assert G.at_or_above(code, "SVP") is True

    @pytest.mark.parametrize("code", NOT_ENTITLED)
    def test_below_svp(self, code: str) -> None:
        assert G.at_or_above(code, "SVP") is False

    @pytest.mark.parametrize("code", UNKNOWN)
    def test_unknown_is_never_entitled(self, code: str | None) -> None:
        """Only an exact stored code counts — a title or lower-case string is unknown."""
        assert G.at_or_above(code, "SVP") is None

    def test_threshold_off_the_ladder_is_unknown(self) -> None:
        assert G.at_or_above("CSUITE", "EVP") is None

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("svp", "SVP"),
            ("Senior Vice President", "SVP"),
            ("  senior   ic ", "SENIOR_IC"),
            ("Senior-IC", "SENIOR_IC"),
            ("C-suite", "CSUITE"),
            ("intern", "INTERN"),
        ],
    )
    def test_normalize_accepts_codes_and_titles(self, raw: str, code: str) -> None:
        assert G.normalize_grade(raw) == code

    @pytest.mark.parametrize("raw", ["", "  ", "EVP", "Chief", "admin"])
    def test_normalize_refuses_anything_else(self, raw: str) -> None:
        with pytest.raises(ValueError):
            G.normalize_grade(raw)

    def test_entitlements_are_all_svp_and_above(self) -> None:
        assert {e.clause_id for e in G.ENTITLEMENTS} == {"AIR-04", "AIR-07", "LOD-05", "NR-01"}
        # Follow-up (grades, intended change): AIR-07 / LOD-05 / NR-01 are rule-enforced too.
        assert [e.clause_id for e in G.ENTITLEMENTS if e.rule_enforced] == [
            "AIR-04",
            "AIR-07",
            "LOD-05",
            "NR-01",
        ]
        assert [g.code for g in G.grades_at_or_above("SVP")] == ["CSUITE", "SVP"]


# ---------------------------------------------------------------------------
# AIR-04 finding (trip approval + planner notices)
# ---------------------------------------------------------------------------


class TestRoleCabinExceptionByGrade:
    @pytest.mark.parametrize("code", ENTITLED)
    @pytest.mark.parametrize("hours", [6.01, 7.0, 9.99])
    def test_entitled_grade_has_no_flag(self, code: str, hours: float) -> None:
        assert (
            pr.role_cabin_exception(
                CABIN, cabin="business", duration_hours=hours, traveller_grade=code
            )
            is None
        )

    @pytest.mark.parametrize("code", NOT_ENTITLED)
    def test_junior_grade_needs_the_cabin_exception(self, code: str) -> None:
        f = pr.role_cabin_exception(CABIN, cabin="business", duration_hours=8, traveller_grade=code)
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED and f.clause_ref == "AIR-04"
        # The AIR-CABIN override approver (VP), not an SVP confirmation of an unknown grade.
        assert f.requires_approver == "VP"
        assert G.title_of(code) in f.reason and "does not cover this seat" in f.reason
        assert f.observed["traveller_grade"] == code

    @pytest.mark.parametrize("code", UNKNOWN)
    def test_unknown_grade_fails_closed_to_an_svp_approver(self, code: str | None) -> None:
        f = pr.role_cabin_exception(CABIN, cabin="business", duration_hours=8, traveller_grade=code)
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED
        assert f.requires_approver == "SVP" and "No grade is recorded" in f.reason
        assert f.observed["could_not_verify"] == "traveller_grade"
        assert f.observed["traveller_grade"] is None

    @pytest.mark.parametrize("hours", [3.0, 6.0])
    def test_six_hours_or_less_is_not_air04_even_for_svp(self, hours: float) -> None:
        for code in (*ENTITLED, *NOT_ENTITLED, None):
            assert (
                pr.role_cabin_exception(
                    CABIN, cabin="business", duration_hours=hours, traveller_grade=code
                )
                is None
            )

    def test_first_class_is_never_granted(self) -> None:
        """AIR-04 grants business only; first class stays AIR-03 'never reimbursable'."""
        status = pr.role_exception_status(
            CABIN, cabin="first", duration_hours=8, traveller_grade="CSUITE"
        )
        assert status.applies is False

    def test_rule_role_decides_the_threshold(self) -> None:
        """A finance-edited rule naming VP moves the threshold (ladder rank, not a literal)."""
        vp_rule = {**CABIN, "role_exception_role": "VP"}
        assert (
            pr.role_cabin_exception(
                vp_rule, cabin="business", duration_hours=8, traveller_grade="VP"
            )
            is None
        )
        f = pr.role_cabin_exception(
            vp_rule, cabin="business", duration_hours=8, traveller_grade="DIRECTOR"
        )
        assert f is not None and f.requires_approver == "VP"

    def test_rule_role_off_the_ladder_fails_closed(self) -> None:
        """A threshold that is not a grade code (refused at activation, but a legacy row
        could hold one) never grants the exception — an approver confirms it."""
        odd = {**CABIN, "role_exception_role": "Chief Pilot"}
        f = pr.role_cabin_exception(odd, cabin="business", duration_hours=8, traveller_grade="SVP")
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED
        assert f.requires_approver == "Chief Pilot" and "not on the grade ladder" in f.reason
        v = PolicyEvalTool()._evaluate_rule(  # noqa: SLF001
            PolicyRule(
                id=uuid.uuid4(),
                policy_id=uuid.uuid4(),
                rule_type=PolicyRuleType.cabin_class,
                applies_to=PolicyAppliesTo.flight,
                definition=odd,
                source_clause_ref="AIR-CABIN",
            ),
            EvalItem(
                item_type="flight",
                cabin_class="business",
                duration_hours=8.0,
                traveller_grade="CSUITE",
            ),
        )
        assert v.in_policy is False and "not on the grade ladder" in v.reason


class TestPlannerNotices:
    def _rules(self) -> process_checks.ProcessRules:
        return process_checks.ProcessRules(
            cabin=(process_checks.RuleRef("r1", "cabin_class", CABIN),)
        )

    def test_svp_traveller_gets_no_air04_notice(self) -> None:
        found, _ = process_checks.option_notices(
            self._rules(),
            flights=[("o1", "business", 900, 8.0)],
            departure=None,
            origin="JFK",
            destination="LHR",
            traveller_grade="SVP",
        )
        assert "o1" not in found

    @pytest.mark.parametrize("grade", ["VP", None])
    def test_other_travellers_get_the_air04_notice(self, grade: str | None) -> None:
        found, _ = process_checks.option_notices(
            self._rules(),
            flights=[("o1", "business", 900, 8.0)],
            departure=None,
            origin="JFK",
            destination="LHR",
            traveller_grade=grade,
        )
        assert [f.clause_ref for f in found["o1"]] == ["AIR-04"]


# ---------------------------------------------------------------------------
# AIR-CABIN verdict (expense lines, trip segments, planner options)
# ---------------------------------------------------------------------------


def _cabin_rule() -> PolicyRule:
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.cabin_class,
        applies_to=PolicyAppliesTo.flight,
        definition=dict(CABIN),
        source_clause_ref="AIR-CABIN",
    )


def _verdict(cabin: str, hours: float, grade: str | None) -> Any:
    item = EvalItem(
        item_type="flight", cabin_class=cabin, duration_hours=hours, traveller_grade=grade
    )
    return PolicyEvalTool()._evaluate_rule(_cabin_rule(), item)  # noqa: SLF001


class TestCabinVerdict:
    @pytest.mark.parametrize("code", ENTITLED)
    def test_entitled_business_is_in_policy_with_an_air04_note(self, code: str) -> None:
        v = _verdict("business", 8.0, code)
        assert v.in_policy is True and v.requires_approver is None
        assert "AIR-04" in v.reason and G.title_of(code) in v.reason
        assert v.observed["role_exception"] == "AIR-04"

    @pytest.mark.parametrize("code", NOT_ENTITLED)
    def test_junior_business_stays_a_violation_citing_air04(self, code: str) -> None:
        v = _verdict("business", 8.0, code)
        assert v.in_policy is False and v.requires_approver == "VP"
        assert "AIR-04" in v.reason and "does not apply" in v.reason

    @pytest.mark.parametrize("code", UNKNOWN)
    def test_unknown_grade_stays_a_violation(self, code: str | None) -> None:
        v = _verdict("business", 8.0, code)
        assert v.in_policy is False and "no grade is recorded" in v.reason

    def test_first_class_stays_a_violation_for_csuite(self) -> None:
        v = _verdict("first", 12.0, "CSUITE")
        assert v.in_policy is False and "AIR-04" not in v.reason

    def test_short_business_stays_a_violation_for_svp(self) -> None:
        v = _verdict("business", 5.0, "SVP")
        assert v.in_policy is False and "AIR-04" not in v.reason

    def test_reason_unchanged_where_air04_does_not_reach(self) -> None:
        """Byte-compatible with the pre-grade wording for non-AIR-04 cases."""
        v = _verdict("premium_economy", 5.0, None)
        assert v.reason == (
            "Cabin class 'premium_economy' is NOT permitted for 5.0h flight. "
            "Allowed: economy. Requires VP override."
        )
        assert _verdict("economy", 8.0, None).reason == (
            "Cabin class 'economy' is permitted for 8.0h flight."
        )

    def test_malformed_air04_keys_fail_closed(self) -> None:
        rule = _cabin_rule()
        rule.definition = {**CABIN, "role_exception_min_hours": "six"}
        item = EvalItem(
            item_type="flight", cabin_class="business", duration_hours=8.0, traveller_grade="SVP"
        )
        v = PolicyEvalTool()._evaluate_rule(rule, item)  # noqa: SLF001
        assert v.in_policy is False


# ---------------------------------------------------------------------------
# Trust boundary, validation, narrative
# ---------------------------------------------------------------------------


def test_what_if_context_can_never_claim_a_grade() -> None:
    from backend.app.modules.policy.router import _ALLOWED_EVAL_CONTEXT_KEYS

    assert "traveller_grade" not in _ALLOWED_EVAL_CONTEXT_KEYS
    assert "cabin_class" in _ALLOWED_EVAL_CONTEXT_KEYS  # the filter itself still works


class TestRuleValidation:
    def test_seeded_cabin_rule_is_valid(self) -> None:
        assert sem.validate_role_exception(CABIN) == []

    @pytest.mark.parametrize("role", ["Chief Pilot", "svp", "EVP"])
    def test_role_must_be_a_ladder_code(self, role: str) -> None:
        errors = sem.validate_role_exception({**CABIN, "role_exception_role": role})
        assert any("grade code" in e for e in errors), errors

    def test_description_names_the_grade_of_record(self) -> None:
        text = " ".join(sem.modifier_sentences("cabin_class", CABIN))
        assert "grade of record" in text and "Atlas does not record grade" not in text


def test_narrative_keeps_the_process_notices() -> None:
    """The LLM narrative step used to rebuild each option field by field and dropped the
    AIR-04 / AIR-06 / PTA-02 notices and the AIR-06 baseline."""

    class _Provider:
        def complete(self, messages: Any) -> Any:
            return type("R", (), {"text": "Because."})()

    notice = {
        "clause_ref": "AIR-04",
        "flag_type": "approval_required",
        "message": "m",
        "requires_approver": "SVP",
        "blocking": False,
    }
    opt = RankedOption(
        option_id="o1",
        segment_type="flight",
        supplier="United Airlines",
        amount_usd=900.0,
        sustainability_score=2,
        cabin_class="business",
        duration_hours=8.0,
        is_direct=True,
        city=None,
        nightly_rate_usd=None,
        room_type=None,
        in_policy=False,
        policy_reason="r",
        clause_refs=("AIR-CABIN", "AIR-04"),
        requires_approver="VP",
        rule_in_policy=False,
        rank_score=1.0,
        explanation="templated",
        lowest_fare_usd=850.0,
        policy_notices=(notice,),
    )
    (out,), degraded = enrich_with_narratives([opt], provider=_Provider())  # type: ignore[arg-type]
    assert degraded is False and out.explanation == "Because."
    assert out.policy_notices == (notice,) and out.lowest_fare_usd == 850.0
