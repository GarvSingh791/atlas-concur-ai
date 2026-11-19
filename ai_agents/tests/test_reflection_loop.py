"""Hermetic tests for the generator→critic reflection loop (FR-A3, P4-T3).

No database, no network.  All tests use the LOCAL LlmProvider or injected
fakes.  Tests mirror the citation-contract hermetic layout from P4-T2.

Fixtures covered
----------------
1. Paris paradox          — "Paris, France" hypothesis + Dallas-area (TX) facts
                            ⇒ outcome revised, correction asserted, ≥1 citation.
2. Correct hypothesis     — "Paris, TX" + TX facts ⇒ passed; NOT over-corrected.
3. Unsupportable (no facts)— unknown city, empty GroundingFacts ⇒ rejected/defer;
                            asserted=False, needs_review=True, value not asserted.
4. ProviderError degrades  — non-location value + _RaisingProvider ⇒ rejected +
                            needs_review; no crash.
5. max_rounds exhaustion  — regenerate ignores correction ⇒ loop stops at
                            max_rounds, outcome=rejected, rounds==max_rounds.
6. Outcome maps to enum   — res.outcome ∈ ReflectionOutcome; same object as
                            database.models.ai_decision_log.ReflectionOutcome.
7. Deterministic no-LLM   — GroundingCritic(provider=None) catches Paris
                            contradiction (Stage 1 only, no LLM required).
8. validate_grounding pass — revised result satisfies validate_grounding contract
                            (asserted=True, ≥1 citation, confidence≥0.7).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    GroundedResult,
    validate_grounding,
)
from ai_agents.guardrails.reflection import (
    DEFAULT_MAX_ROUNDS,
    Critique,
    GroundingCritic,
    GroundingFacts,
    LocationClaim,
    LocationFact,
    reflect,
)
from database.models.ai_decision_log import ReflectionOutcome
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Always raises ProviderError — simulates LLM unavailability."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise ProviderError("LLM unavailable in test", code="PROVIDER_UNAVAILABLE")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise ProviderError("LLM unavailable in test", code="PROVIDER_UNAVAILABLE")


class _ScriptedProvider(LlmProvider):
    """Returns a fixed text response — for LLM Stage 2 tests."""

    def __init__(self, response_text: str) -> None:
        self._text = response_text

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        return LlmResponse(
            text=self._text,
            model="test-stub",
            usage=LlmUsage(input_tokens=10, output_tokens=5),
            tool_calls=[],
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield LlmChunk(delta=self._text)


@dataclass
class _SimpleHypothesizer:
    """Seed a fixed hypothesis; regenerate by adopting critique.correction."""

    _seed: GroundedResult

    def seed(self) -> GroundedResult:
        return self._seed

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult:
        if critique.correction is not None:
            return critique.correction
        return previous


@dataclass
class _IgnorantHypothesizer:
    """Always regenerates back to the original seed (ignores correction).

    Used to test max_rounds exhaustion — critic will keep contradicting and
    the hypothesizer will never improve.
    """

    _seed: GroundedResult

    def seed(self) -> GroundedResult:
        return self._seed

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult:
        return self._seed  # always the same wrong answer


def _paris_france_hypothesis() -> GroundedResult:
    return GroundedResult(
        claim="Location of expense",
        value=LocationClaim(city="Paris", country="France"),
        citations=(),
        confidence=0.6,
        asserted=False,
    )


def _paris_tx_hypothesis() -> GroundedResult:
    return GroundedResult(
        claim="Location of expense",
        value=LocationClaim(city="Paris", region="TX", country="US"),
        citations=(),
        confidence=0.6,
        asserted=False,
    )


def _dallas_area_facts() -> GroundingFacts:
    """Injected grounding: the trip is a Dallas-area (TX) itinerary."""
    return GroundingFacts(
        locations=(
            LocationFact(city="Dallas", region="TX", country="US", ref="trip.leg.0"),
            LocationFact(city="Paris", region="TX", country="US", ref="trip.leg.1"),
        ),
        source="trip itinerary",
    )


# ---------------------------------------------------------------------------
# 1. Paris paradox
# ---------------------------------------------------------------------------


class TestParisParadox:
    def test_france_hypothesis_is_revised_to_tx(self) -> None:
        """Core fixture: Paris/France + Dallas-area grounding ⇒ revised, never silent pass."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome == ReflectionOutcome.revised
        assert result.asserted is True

    def test_revised_result_has_citation(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert len(result.citations) >= 1

    def test_revised_result_passes_validate_grounding(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        validated = validate_grounding(result.grounded, threshold=DEFAULT_CITATION_THRESHOLD)
        assert validated.asserted is True

    def test_revised_value_is_paris_tx_not_france(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert isinstance(result.value, LocationClaim)
        loc: LocationClaim = result.value  # type: ignore[assignment]
        assert loc.city == "Paris"
        assert loc.region == "TX" or loc.country != "France"

    def test_france_is_never_a_silent_pass(self) -> None:
        """The loop must NOT return 'passed' for a wrong-country hypothesis."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome != ReflectionOutcome.passed


# ---------------------------------------------------------------------------
# 2. Correct hypothesis passes (no over-correction)
# ---------------------------------------------------------------------------


class TestCorrectHypothesisPasses:
    def test_paris_tx_returns_passed(self) -> None:
        """A correct hypothesis consistent with grounding returns 'passed'."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome == ReflectionOutcome.passed

    def test_correct_hypothesis_is_asserted(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.asserted is True

    def test_correct_hypothesis_not_revised(self) -> None:
        """passed ≠ revised — a right answer must not be relabelled as revised."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome is not ReflectionOutcome.revised

    def test_correct_hypothesis_has_citation(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert len(result.citations) >= 1


# ---------------------------------------------------------------------------
# 3. Unsupportable hypothesis (no matching grounding)
# ---------------------------------------------------------------------------


class TestUnsupportableHypothesis:
    def test_unknown_city_empty_facts_is_rejected(self) -> None:
        """No grounding available ⇒ rejected/defer, not guessed."""
        empty_facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=empty_facts, provider=None)
        seed = GroundedResult(
            claim="Location of expense",
            value=LocationClaim(city="Atlantis"),
            citations=(),
            confidence=0.5,
            asserted=False,
        )
        hyp = _SimpleHypothesizer(seed)

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome == ReflectionOutcome.rejected

    def test_rejected_result_not_asserted(self) -> None:
        empty_facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=empty_facts, provider=None)
        seed = GroundedResult(
            claim="Location of expense",
            value=LocationClaim(city="Atlantis"),
            citations=(),
            confidence=0.5,
            asserted=False,
        )
        hyp = _SimpleHypothesizer(seed)

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.asserted is False

    def test_rejected_result_needs_review(self) -> None:
        empty_facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=empty_facts, provider=None)
        seed = GroundedResult(
            claim="Location of expense",
            value=LocationClaim(city="Atlantis"),
            citations=(),
            confidence=0.5,
            asserted=False,
        )
        hyp = _SimpleHypothesizer(seed)

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.needs_review is True


# ---------------------------------------------------------------------------
# 4. LLM ProviderError degrades gracefully (NFR-R1)
# ---------------------------------------------------------------------------


class TestProviderErrorDegrades:
    def _non_location_hypothesis(self) -> GroundedResult:
        """Non-LocationClaim value forces Stage 2 (LLM) path."""
        return GroundedResult(
            claim="Vendor is compliant",
            value={"vendor_id": "v-42", "approved": True},
            citations=(),
            confidence=0.5,
            asserted=False,
        )

    def test_provider_error_does_not_crash(self) -> None:
        """ProviderError ⇒ graceful degradation, no exception propagated."""
        facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=facts, provider=_RaisingProvider())
        hyp = _SimpleHypothesizer(self._non_location_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        # Should not raise; outcome must be rejected (safe default).
        assert result.outcome == ReflectionOutcome.rejected

    def test_provider_error_sets_needs_review(self) -> None:
        facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=facts, provider=_RaisingProvider())
        hyp = _SimpleHypothesizer(self._non_location_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.needs_review is True

    def test_provider_error_result_not_asserted(self) -> None:
        facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=facts, provider=_RaisingProvider())
        hyp = _SimpleHypothesizer(self._non_location_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.asserted is False


# ---------------------------------------------------------------------------
# 5. max_rounds exhaustion — loop stops, does not spin forever
# ---------------------------------------------------------------------------


class TestMaxRoundsExhaustion:
    def test_ignorant_hypothesizer_stops_at_max_rounds(self) -> None:
        """A hypothesizer that never improves must stop at max_rounds."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _IgnorantHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic, max_rounds=3)

        assert result.rounds == 3

    def test_ignorant_hypothesizer_outcome_is_rejected(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _IgnorantHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic, max_rounds=3)

        assert result.outcome == ReflectionOutcome.rejected

    def test_max_rounds_one_still_terminates(self) -> None:
        """max_rounds=1 processes one round and stops."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _IgnorantHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic, max_rounds=1)

        assert result.rounds <= 1

    def test_max_rounds_zero_rejects_immediately(self) -> None:
        """max_rounds=0 ⇒ rejected immediately (no iterations permitted)."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic, max_rounds=0)

        assert result.outcome == ReflectionOutcome.rejected
        assert result.rounds == 0


# ---------------------------------------------------------------------------
# 6. Outcome maps to the database enum (same object — not a fork)
# ---------------------------------------------------------------------------


class TestOutcomeMapsToDbEnum:
    def test_passed_is_reflection_outcome_passed(self) -> None:
        from database.models.ai_decision_log import ReflectionOutcome as DbReflectionOutcome

        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome is DbReflectionOutcome.passed

    def test_revised_is_reflection_outcome_revised(self) -> None:
        from database.models.ai_decision_log import ReflectionOutcome as DbReflectionOutcome

        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome is DbReflectionOutcome.revised

    def test_rejected_is_reflection_outcome_rejected(self) -> None:
        from database.models.ai_decision_log import ReflectionOutcome as DbReflectionOutcome

        empty_facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=empty_facts, provider=None)
        seed = GroundedResult(
            claim="Location",
            value=LocationClaim(city="Atlantis"),
            citations=(),
            confidence=0.5,
            asserted=False,
        )
        hyp = _SimpleHypothesizer(seed)

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome is DbReflectionOutcome.rejected

    def test_all_enum_values_covered(self) -> None:
        assert set(ReflectionOutcome) == {
            ReflectionOutcome.passed,
            ReflectionOutcome.revised,
            ReflectionOutcome.rejected,
        }


# ---------------------------------------------------------------------------
# 7. Deterministic critic alone (no LLM) catches Paris paradox
# ---------------------------------------------------------------------------


class TestGroundingCriticDeterministic:
    def test_stage1_catches_france_contradiction(self) -> None:
        """GroundingCritic with provider=None catches the Paris paradox in Stage 1."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hypothesis = _paris_france_hypothesis()

        critique = critic(hypothesis)

        assert critique.verdict == "contradicted"
        assert critique.correction is not None

    def test_stage1_supports_correct_tx_claim(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hypothesis = _paris_tx_hypothesis()

        critique = critic(hypothesis)

        assert critique.verdict == "supported"

    def test_stage1_unsupported_when_city_absent(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hypothesis = GroundedResult(
            claim="Location",
            value=LocationClaim(city="Honolulu"),
            citations=(),
            confidence=0.5,
            asserted=False,
        )

        critique = critic(hypothesis)

        assert critique.verdict == "unsupported"

    def test_stage1_correction_carries_citation(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hypothesis = _paris_france_hypothesis()

        critique = critic(hypothesis)

        assert critique.correction is not None
        assert len(critique.correction.citations) >= 1

    def test_no_provider_for_non_location_returns_unsupported_needs_review(self) -> None:
        """When provider=None and value is not LocationClaim, Stage 2 degrades."""
        facts = GroundingFacts(locations=(), source=None)
        critic = GroundingCritic(facts=facts, provider=None)
        hypothesis = GroundedResult(
            claim="Vendor check",
            value={"vendor": "Acme"},
            citations=(),
            confidence=0.5,
            asserted=False,
        )

        critique = critic(hypothesis)

        assert critique.verdict == "unsupported"
        assert critique.needs_review is True


# ---------------------------------------------------------------------------
# 8. Validate-grounding contract on revised result
# ---------------------------------------------------------------------------


class TestRevisedResultGroundingContract:
    def test_revised_grounded_satisfies_contract(self) -> None:
        """Revised GroundedResult must carry ≥1 citation AND confidence ≥ threshold."""
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert result.outcome == ReflectionOutcome.revised
        assert result.grounded.confidence >= DEFAULT_CITATION_THRESHOLD
        assert len(result.grounded.citations) >= 1
        assert result.grounded.asserted is True

    def test_result_value_is_location_claim(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_france_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        assert isinstance(result.value, LocationClaim)

    def test_reflection_result_convenience_properties(self) -> None:
        facts = _dallas_area_facts()
        critic = GroundingCritic(facts=facts, provider=None)
        hyp = _SimpleHypothesizer(_paris_tx_hypothesis())

        result = reflect(hypothesizer=hyp, critic=critic)

        # Convenience properties delegate to .grounded correctly.
        assert result.value is result.grounded.value
        assert result.asserted is result.grounded.asserted
        assert result.citations is result.grounded.citations


# ---------------------------------------------------------------------------
# 9. DEFAULT_MAX_ROUNDS is the expected value
# ---------------------------------------------------------------------------


def test_default_max_rounds_is_three() -> None:
    assert DEFAULT_MAX_ROUNDS == 3
