"""Generator→critic reflection loop (FR-A3).

Implements the "Paris paradox" defence: a seed hypothesis is critiqued against
injected grounding facts; if contradicted, it is revised (or rejected) before
the value is committed.  The loop is generic over an injected Hypothesizer
(seed / regenerate strategy) and Critic, so both the travel-recommendation
agent (P4-T9) and the receipt gap-fill pipeline (P4-T11) can reuse it.

Types
-----
ReflectionOutcome — re-export of database.models ReflectionOutcome enum
                    (passed | revised | rejected); 1:1 with
                    ai_decision_logs.reflection_outcome.
CriticVerdict     — deterministic verdict from the critic for one hypothesis.
Critique          — critic output: verdict + rationale + optional correction.
ReflectionResult  — final loop output: outcome, grounded result, rationale,
                    needs_review flag, round count.
LocationClaim     — hypothesis value shape the deterministic critic understands.
LocationFact      — one verified location fact in the injected GroundingFacts.
GroundingFacts    — container of injected facts for the GroundingCritic.
GroundingCritic   — reference critic: deterministic Stage 1 (no LLM) +
                    optional LLM Stage 2 with graceful ProviderError degradation.

Functions
---------
reflect()         — the bounded generator→critic loop.

Decision-log seam (return, don't write)
----------------------------------------
The loop returns ReflectionResult; it does NOT open a session or write to the
database.  Callers (P4-T9 recommendation, P4-T11 gap-fill) map
result.outcome → DecisionRecord.reflection_outcome and call write_decision_log
inside their own transaction.

    from ai_agents.decision_log import DecisionRecord, write_decision_log
    rec = DecisionRecord(
        ...
        reflection_outcome=result.outcome,
        confidence=result.grounded.confidence,
        output_summary=result.grounded.to_dict(),
    )
    write_decision_log(session, context=ctx, record=rec)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    Citation,
    CitationKind,
    GroundedResult,
    validate_grounding,
)
from database.models.ai_decision_log import ReflectionOutcome
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ROUNDS: int = 3
"""Maximum generator→critic iterations before the loop defers (rejects)."""

_GROUNDED_MATCH_CONFIDENCE: float = 0.95
"""Confidence assigned when a location claim matches injected grounding facts."""

# ---------------------------------------------------------------------------
# Critic verdict
# ---------------------------------------------------------------------------

CriticVerdict = Literal["supported", "contradicted", "unsupported"]


@dataclass(frozen=True)
class Critique:
    """Output of one critic evaluation of a hypothesis.

    verdict == "supported"    — hypothesis is consistent with grounding; no
                                correction needed.
    verdict == "contradicted" — hypothesis conflicts with grounding; correction
                                may be attached (GroundedResult with the revised
                                value and a supporting citation).
    verdict == "unsupported"  — grounding is absent or inconclusive; the critic
                                cannot either support or refute the hypothesis.
    """

    verdict: CriticVerdict
    rationale: str
    correction: GroundedResult | None = None
    """Present when verdict == "contradicted" and a corrected value is known."""
    needs_review: bool = False
    """True when the critic could not run deterministically (e.g. LLM degraded)."""


# ---------------------------------------------------------------------------
# Reflection result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReflectionResult:
    """Final output of the reflect() loop.

    outcome    — maps 1:1 to ai_decision_logs.reflection_outcome
                 (passed | revised | rejected).
    grounded   — the final GroundedResult value.
                 For passed/revised: asserted=True, ≥1 citation.
                 For rejected: asserted=False (safe default — not presented to
                 the user; deferred for clarification).
    critique   — the rationale of the deciding critique.
    needs_review — True when the LLM critic degraded or the loop rejected;
                   signals the caller to flag the output for human review.
    rounds     — number of generator→critic iterations consumed.
    """

    outcome: ReflectionOutcome
    grounded: GroundedResult
    critique: str
    needs_review: bool
    rounds: int
    # C7-06: True when the DECIDING critique was "supported" — i.e. the critic verified the
    # draft against the retrieved clauses.  Distinguishes a draft that only failed the
    # citation-contract confidence floor (outcome=rejected, critic_supported=True) from one the
    # critic actually rejected (unsupported/contradicted/degraded → False).  Additive; default
    # False keeps every existing construction site (and the safe default) unchanged.
    critic_supported: bool = False

    @property
    def value(self) -> object:
        return self.grounded.value

    @property
    def asserted(self) -> bool:
        return self.grounded.asserted

    @property
    def citations(self) -> tuple[Citation, ...]:
        return self.grounded.citations


# ---------------------------------------------------------------------------
# Injected-strategy protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Hypothesizer(Protocol):
    """Generates and revises the hypothesis value.

    seed()       — returns the initial hypothesis as a GroundedResult.
    regenerate() — given the previous hypothesis and a critique carrying a
                   correction, returns a revised hypothesis.  Called only when
                   verdict == "contradicted" and correction is not None.
    """

    def seed(self) -> GroundedResult: ...

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult: ...


@runtime_checkable
class Critic(Protocol):
    """Evaluates one hypothesis against some grounding source."""

    def __call__(self, hypothesis: GroundedResult) -> Critique: ...


# ---------------------------------------------------------------------------
# The bounded loop
# ---------------------------------------------------------------------------


def reflect(
    *,
    hypothesizer: Hypothesizer,
    critic: Critic,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    threshold: float = DEFAULT_CITATION_THRESHOLD,
) -> ReflectionResult:
    """Run the generator→critic reflection loop.

    The loop is bounded by *max_rounds* to prevent infinite generator↔critic
    spin.  On exhaustion without a supported result the loop returns a
    rejected/defer result — the safe default (never a guess).

    Args:
        hypothesizer: Injected seed + regenerate strategy.
        critic:       Injected critic (facts are bound into the critic).
        max_rounds:   Maximum iterations (default DEFAULT_MAX_ROUNDS=3).
        threshold:    Confidence threshold for validate_grounding (default
                      DEFAULT_CITATION_THRESHOLD=0.7).

    Returns:
        ReflectionResult with outcome passed|revised|rejected, the final
        GroundedResult, critique rationale, needs_review flag, and round count.

    Never raises (critic ProviderError is caught by GroundingCritic; a critic
    that always contradicts or never resolves exhausts max_rounds and returns
    rejected/defer).
    """
    if max_rounds < 1:
        return _rejected(
            grounded=hypothesizer.seed(),
            rationale="max_rounds < 1; no iterations permitted.",
            needs_review=True,
            rounds=0,
        )

    hypothesis: GroundedResult = hypothesizer.seed()
    was_revised: bool = False
    accumulated_needs_review: bool = False
    last_rationale: str = ""

    for round_num in range(1, max_rounds + 1):
        c: Critique = critic(hypothesis)
        accumulated_needs_review |= c.needs_review
        last_rationale = c.rationale

        if c.verdict == "supported":
            # The critic may have enhanced the hypothesis with a supporting
            # citation (returned via c.correction).  Use that enriched version
            # so validate_grounding has ≥1 citation to promote asserted=True.
            candidate = c.correction if c.correction is not None else hypothesis
            validated = validate_grounding(candidate, threshold=threshold)
            if validated.asserted:
                outcome = ReflectionOutcome.revised if was_revised else ReflectionOutcome.passed
                logger.debug(
                    "reflect: %s after %d round(s); confidence=%.3f citations=%d",
                    outcome.value,
                    round_num,
                    validated.confidence,
                    len(validated.citations),
                )
                return ReflectionResult(
                    outcome=outcome,
                    grounded=validated,
                    critique=c.rationale,
                    needs_review=accumulated_needs_review,
                    rounds=round_num,
                    critic_supported=True,
                )
            # Supported by critic but grounding contract fails (no citation /
            # below threshold) — treat as unsupported: defer.
            logger.debug(
                "reflect: critic said supported but validate_grounding failed "
                "(confidence=%.3f, citations=%d); rejecting round %d",
                candidate.confidence,
                len(candidate.citations),
                round_num,
            )
            return _rejected(
                grounded=candidate,
                rationale=(
                    f"Critic supported the hypothesis but grounding contract "
                    f"failed (confidence={candidate.confidence:.3f}, "
                    f"citations={len(candidate.citations)}). {c.rationale}"
                ),
                needs_review=accumulated_needs_review,
                rounds=round_num,
                critic_supported=True,
            )

        if c.verdict == "contradicted" and c.correction is not None:
            was_revised = True
            logger.debug(
                "reflect: contradiction in round %d; regenerating with correction",
                round_num,
            )
            hypothesis = hypothesizer.regenerate(hypothesis, c)
            continue

        # "unsupported" or "contradicted" with no correction — defer.
        logger.debug(
            "reflect: verdict=%s, no correction available in round %d; rejecting",
            c.verdict,
            round_num,
        )
        return _rejected(
            grounded=hypothesis,
            rationale=c.rationale,
            needs_review=accumulated_needs_review,
            rounds=round_num,
        )

    # max_rounds exhausted without reaching a supported conclusion.
    logger.debug(
        "reflect: max_rounds=%d exhausted without a supported hypothesis; rejecting",
        max_rounds,
    )
    return _rejected(
        grounded=hypothesis,
        rationale=(
            f"max_rounds ({max_rounds}) exhausted without a supported hypothesis. "
            f"Last critique: {last_rationale}"
        ),
        needs_review=True,
        rounds=max_rounds,
    )


def _rejected(
    *,
    grounded: GroundedResult,
    rationale: str,
    needs_review: bool,
    rounds: int,
    critic_supported: bool = False,
) -> ReflectionResult:
    """Return a safe-default rejected ReflectionResult (asserted=False)."""
    not_asserted = GroundedResult(
        claim=grounded.claim,
        value=grounded.value,
        citations=grounded.citations,
        confidence=grounded.confidence,
        asserted=False,
    )
    return ReflectionResult(
        outcome=ReflectionOutcome.rejected,
        grounded=not_asserted,
        critique=rationale,
        needs_review=True,  # always needs_review on rejection
        rounds=rounds,
        critic_supported=critic_supported,
    )


# ---------------------------------------------------------------------------
# Location types for the reference GroundingCritic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocationClaim:
    """A hypothesis about a geographic location.

    The GroundingCritic understands this value type for deterministic Stage 1
    matching.  Other hypothesis value types fall through to Stage 2 (LLM).
    """

    city: str
    region: str | None = None
    """State/province/region abbreviation or name (e.g. "TX", "Texas")."""
    country: str | None = None
    """Country name or code (e.g. "US", "France")."""


@dataclass(frozen=True)
class LocationFact:
    """One verified location in the injected grounding context.

    ref: evidence pointer — used as Citation.ref when building a correction.
    """

    city: str
    region: str | None = None
    country: str | None = None
    ref: str = "itinerary"
    """Evidence pointer used as Citation.ref."""


@dataclass(frozen=True)
class GroundingFacts:
    """Container of injected grounding facts for the GroundingCritic.

    Grounding is INJECTED (not fetched live); P4-T4 tool layer fetches and
    passes facts into this container.  GroundingCritic stays pure / testable
    without any live data source.
    """

    locations: tuple[LocationFact, ...] = ()
    source: str | None = None
    """Human-readable source label (e.g. "trip itinerary", "calendar") used in
    Citation.source."""


# ---------------------------------------------------------------------------
# Reference critic — GroundingCritic
# ---------------------------------------------------------------------------


@dataclass
class GroundingCritic:
    """Deterministic-first critic with an optional LLM Stage 2 fallback.

    Stage 1 — deterministic grounding comparison (no LLM required).
        If hypothesis.value is a LocationClaim, match its city (case-insensitive)
        against facts.locations:
          - City found, region/country consistent → supported (+ citation).
          - City found, region/country conflicts  → contradicted + correction
            (Paris paradox: "Paris, France" vs Dallas-area trip → corrected to
            the fact's region/country with a source_field citation).
          - No matching city in facts             → unsupported (needs_review if
            provider is None).

    Stage 2 — LLM critic (only when Stage 1 is inconclusive, i.e. hypothesis
        value is NOT a LocationClaim).  If provider is None or raises
        ProviderError, degrade gracefully: return unsupported + needs_review
        (NFR-R1).  Never crash; never guess.

    The critic implements the Critic protocol (callable).
    """

    facts: GroundingFacts
    provider: LlmProvider | None = None

    def __call__(self, hypothesis: GroundedResult) -> Critique:
        if isinstance(hypothesis.value, LocationClaim):
            return self._stage1_deterministic(hypothesis)
        return self._stage2_llm(hypothesis)

    # ------------------------------------------------------------------
    # Stage 1 — deterministic location matching
    # ------------------------------------------------------------------

    def _stage1_deterministic(self, hypothesis: GroundedResult) -> Critique:
        claim: LocationClaim = hypothesis.value  # type: ignore[assignment]

        if not self.facts.locations:
            return Critique(
                verdict="unsupported",
                rationale=(
                    "No location facts available to evaluate the hypothesis "
                    f"(city={claim.city!r}). Deferring for clarification."
                ),
                needs_review=True,
            )

        for fact in self.facts.locations:
            if fact.city.lower() != claim.city.lower():
                continue

            # City matches — check region/country consistency.
            region_conflict = (
                claim.region is not None
                and fact.region is not None
                and claim.region.lower() != fact.region.lower()
            )
            country_conflict = (
                claim.country is not None
                and fact.country is not None
                and claim.country.lower() != fact.country.lower()
            )

            if region_conflict or country_conflict:
                # Contradiction — build a correction toward the grounded fact.
                correction_value = LocationClaim(
                    city=fact.city,
                    region=fact.region,
                    country=fact.country,
                )
                detail_parts = [fact.city]
                if fact.region:
                    detail_parts.append(fact.region)
                if fact.country:
                    detail_parts.append(fact.country)
                correction_citation = Citation(
                    kind=CitationKind.source_field,
                    ref=fact.ref,
                    detail=", ".join(detail_parts),
                    source=self.facts.source,
                )
                correction_claim = (
                    f"Location corrected from {claim.city}, "
                    f"{claim.region or claim.country!r} to "
                    f"{fact.city}, {fact.region or fact.country}"
                )
                correction_grounded = GroundedResult(
                    claim=correction_claim,
                    value=correction_value,
                    citations=(correction_citation,),
                    confidence=_GROUNDED_MATCH_CONFIDENCE,
                    asserted=True,
                )
                conflict_desc = []
                if region_conflict:
                    conflict_desc.append(f"region {claim.region!r} ≠ {fact.region!r}")
                if country_conflict:
                    conflict_desc.append(f"country {claim.country!r} ≠ {fact.country!r}")
                return Critique(
                    verdict="contradicted",
                    rationale=(
                        f"Location hypothesis city={claim.city!r} matched the "
                        f"itinerary but {'; '.join(conflict_desc)}. "
                        f"Correcting to grounded fact: "
                        f"{', '.join(detail_parts)}."
                    ),
                    correction=correction_grounded,
                )

            # City matches AND region/country are consistent (or unspecified).
            support_parts = [fact.city]
            if fact.region:
                support_parts.append(fact.region)
            if fact.country:
                support_parts.append(fact.country)
            support_citation = Citation(
                kind=CitationKind.source_field,
                ref=fact.ref,
                detail=", ".join(support_parts),
                source=self.facts.source,
            )
            # Re-attach the supporting citation to the original hypothesis.
            supported_grounded = GroundedResult(
                claim=hypothesis.claim,
                value=hypothesis.value,
                citations=hypothesis.citations + (support_citation,),
                confidence=_GROUNDED_MATCH_CONFIDENCE,
                asserted=False,  # validate_grounding will promote this
            )
            # Return a supported critique; the loop will validate_grounding.
            return Critique(
                verdict="supported",
                rationale=(
                    f"Location hypothesis city={claim.city!r} is consistent "
                    f"with the grounded fact ({', '.join(support_parts)})."
                ),
                # The loop reads hypothesis; we surface the supported grounded
                # value by returning the correction slot here.
                correction=supported_grounded,
            )

        # City not found in any fact.
        return Critique(
            verdict="unsupported",
            rationale=(
                f"Location city={claim.city!r} was not found in the injected "
                "grounding facts. Cannot confirm or correct the hypothesis."
            ),
            needs_review=True,
        )

    # ------------------------------------------------------------------
    # Stage 2 — optional LLM critic (for non-LocationClaim hypothesis values)
    # ------------------------------------------------------------------

    def _stage2_llm(self, hypothesis: GroundedResult) -> Critique:
        if self.provider is None:
            return Critique(
                verdict="unsupported",
                rationale=(
                    "No LLM provider available for non-location hypothesis; "
                    "deferring for human review (NFR-R1)."
                ),
                needs_review=True,
            )

        prompt = (
            f"You are a fact-checker. Evaluate the following hypothesis against "
            f"the provided context.\n\n"
            f"Hypothesis claim: {hypothesis.claim}\n"
            f"Hypothesis value: {hypothesis.value!r}\n"
            f"Context: {self.facts.source or 'injected grounding facts'}\n"
            f"Location facts: "
            f"{[f'{f.city}, {f.region}, {f.country}' for f in self.facts.locations]}\n\n"
            f"Reply with one word only: SUPPORTED, CONTRADICTED, or UNSUPPORTED. "
            f"Then on the next line, give a short rationale (1 sentence)."
        )
        try:
            response = self.provider.complete([LlmMessage(role="user", content=prompt)])
        except ProviderError as exc:
            logger.warning(
                "reflect: LLM critic raised ProviderError (%s); degrading to "
                "unsupported + needs_review (NFR-R1).",
                exc.code,
            )
            return Critique(
                verdict="unsupported",
                rationale=(
                    f"LLM critic unavailable (ProviderError: {exc.code}); "
                    "deferring to safe default (NFR-R1)."
                ),
                needs_review=True,
            )

        text = response.text.strip()
        lines = text.splitlines()
        verdict_raw = lines[0].strip().upper() if lines else ""
        rationale = lines[1].strip() if len(lines) > 1 else text

        if verdict_raw.startswith("SUPPORTED"):
            return Critique(verdict="supported", rationale=rationale)
        if verdict_raw.startswith("CONTRADICTED"):
            return Critique(verdict="contradicted", rationale=rationale)
        return Critique(
            verdict="unsupported",
            rationale=rationale or "LLM critic response was inconclusive.",
            needs_review=True,
        )
