"""Receipt reflection step — location-only Paris-paradox defence (P4-T11, FR-A3).

Responsibility
--------------
Only the gap-filled `location` field goes through reflect().  The rest of the
gap-filled fields (amount, vendor, date, expense_type, …) are written with
provenance='ai_inferred' and their LLM-derived confidence directly.

ReceiptFieldHypothesizer
    seed()       → GroundedResult whose .value is a LocationClaim (city/region/country)
                   parsed from the LLM-inferred location string.
    regenerate() → adopts critique.correction when the critic revises the location.

build_grounding_facts(itinerary_result)
    Converts ItineraryResult segments/events → GroundingFacts(locations, source)
    for the reference GroundingCritic.

run_location_reflection
    Calls reflect(hypothesizer, critic) and returns the ReflectionResult.
    Caller (pipeline.py) maps the outcome to the write-mapping below.

Write mapping (pipeline.py concern, documented here for reference)
    outcome=passed   → keep inferred location; provenance=ai_inferred; conf=grounded.confidence
    outcome=revised  → write corrected location; provenance=ai_inferred; needs_review per asserted
    outcome=rejected → set location=None; provenance=ai_inferred; low conf; needs_review=True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_agents.guardrails.citations import GroundedResult
from ai_agents.guardrails.reflection import (
    Critique,
    GroundingCritic,
    GroundingFacts,
    LocationClaim,
    LocationFact,
    ReflectionResult,
    reflect,
)
from ai_agents.tools.itinerary_calendar import ItineraryResult

logger = logging.getLogger(__name__)

_DEFAULT_LOCATION_CONF: float = 0.60
"""Confidence assigned to the initial LLM-inferred location (pre-reflection)."""


# ---------------------------------------------------------------------------
# Grounding facts builder
# ---------------------------------------------------------------------------


def _parse_location_string(location_str: str | None) -> LocationFact | None:
    """Best-effort parse of a 'City, Region' or 'City, Country' string → LocationFact."""
    if not location_str or not location_str.strip():
        return None
    parts = [p.strip() for p in location_str.split(",")]
    city = parts[0] if parts else location_str.strip()
    region = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    if not city:
        return None
    return LocationFact(city=city, region=region, country=country)


def build_grounding_facts(itinerary: ItineraryResult) -> GroundingFacts:
    """Convert ItineraryResult segments + calendar events → GroundingFacts.

    Each segment/event with a non-empty location string is turned into a
    LocationFact.  The ref field carries the segment_id or event_id so the
    critic can build a properly-sourced Citation.
    """
    location_facts: list[LocationFact] = []
    seen: set[str] = set()

    for seg in itinerary.segments:
        fact = _parse_location_string(seg.location)
        if fact is None:
            continue
        key = fact.city.lower()
        if key in seen:
            continue
        seen.add(key)
        location_facts.append(
            LocationFact(
                city=fact.city,
                region=fact.region,
                country=fact.country,
                ref=f"trip_segments.id:{seg.segment_id}",
            )
        )

    for evt in itinerary.calendar_events:
        fact = _parse_location_string(evt.location)
        if fact is None:
            continue
        key = fact.city.lower()
        if key in seen:
            continue
        seen.add(key)
        location_facts.append(
            LocationFact(
                city=fact.city,
                region=fact.region,
                country=fact.country,
                ref=f"calendar_events.id:{evt.event_id}",
            )
        )

    return GroundingFacts(
        locations=tuple(location_facts),
        source="trips",
    )


# ---------------------------------------------------------------------------
# Location claim parser
# ---------------------------------------------------------------------------


def _parse_claim(location_str: str | None) -> LocationClaim:
    """Parse a location string to a LocationClaim (best-effort)."""
    if not location_str or not location_str.strip():
        return LocationClaim(city="unknown")
    parts = [p.strip() for p in location_str.split(",")]
    city = parts[0] if parts else location_str.strip()
    region = parts[1] if len(parts) >= 2 else None
    country = parts[2] if len(parts) >= 3 else None
    return LocationClaim(city=city or "unknown", region=region, country=country)


# ---------------------------------------------------------------------------
# Hypothesizer
# ---------------------------------------------------------------------------


@dataclass
class ReceiptFieldHypothesizer:
    """Seeds the location hypothesis from the LLM-inferred location string.

    seed()       — returns GroundedResult(value=LocationClaim, citations=(), ...)
    regenerate() — adopts critique.correction (the grounded fact from the critic).
    """

    _location_str: str | None
    _confidence: float = _DEFAULT_LOCATION_CONF

    def seed(self) -> GroundedResult:
        claim_value = _parse_claim(self._location_str)
        return GroundedResult(
            claim=f"Location of receipt expense: {self._location_str!r}",
            value=claim_value,
            citations=(),
            confidence=self._confidence,
            asserted=False,
        )

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult:
        if critique.correction is not None:
            return critique.correction
        return previous


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_location_reflection(
    location_str: str | None,
    *,
    itinerary: ItineraryResult,
) -> ReflectionResult:
    """Run the Paris-paradox reflection for the inferred location field.

    Uses the reference GroundingCritic (deterministic Stage 1; no LLM).
    Returns a ReflectionResult whose outcome drives the write mapping in pipeline.py.

    Args:
        location_str: LLM-inferred location string (e.g. "New York, NY").
        itinerary:    ItineraryResult providing grounding facts.

    Returns:
        ReflectionResult (passed | revised | rejected).  Never raises.
    """
    hypothesizer = ReceiptFieldHypothesizer(_location_str=location_str)
    facts = build_grounding_facts(itinerary)
    critic = GroundingCritic(facts=facts, provider=None)

    result = reflect(hypothesizer=hypothesizer, critic=critic)
    logger.debug(
        "receipt_reflection: outcome=%s asserted=%s rounds=%d location=%r",
        result.outcome.value,
        result.asserted,
        result.rounds,
        location_str,
    )
    return result
