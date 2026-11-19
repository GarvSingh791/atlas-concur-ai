"""Trip intent extraction and slot validation (P4-T9, FR-T1/T2)."""

from __future__ import annotations

from dataclasses import dataclass, field

# Required slots that must be present before the agent can proceed (FR-T1).
_REQUIRED_SLOTS: tuple[str, ...] = (
    "origin",
    "destination",
    "departure_date",
    "travelers",
    "currency",
)


@dataclass(frozen=True)
class TripIntent:
    """Structured trip intent extracted from task.payload."""

    # Required slots — None signals a missing / unparseable value
    origin: str | None
    destination: str | None
    departure_date: str | None  # YYYY-MM-DD
    return_date: str | None  # YYYY-MM-DD; None for one-way
    travelers: int | None
    currency: str | None  # ISO 4217

    # Optional trip purpose ("Client meeting") — persisted on Trip.purpose
    purpose: str | None = None

    # Scope flags
    include_flights: bool = True
    include_hotels: bool = True

    # Optional interaction context (supplied by the P4-T13 endpoint)
    conversation_id: str | None = None
    # traveler_id: if present and ≠ context.user_id, the role-bound check blocks it
    traveler_id: str | None = None
    # Triggers the persist-on-selection path (FR-T7)
    selected_option_ids: list[str] | None = None
    # Request optional LLM plain-language narrative (FR-T6 LLM path)
    explain_with_llm: bool = False

    # Free-form constraints (open extension point; not validated by the agent)
    constraints: dict = field(default_factory=dict)


def parse_intent(payload: dict) -> TripIntent:
    """Extract TripIntent from the raw Task.payload dict.

    Absent or unparseable required slots are set to None — the agent then
    enters the clarify path rather than failing hard.
    """
    travelers_raw = payload.get("travelers")
    travelers: int | None = None
    if travelers_raw is not None:
        try:
            v = int(travelers_raw)
            travelers = v if v >= 1 else None
        except (ValueError, TypeError):
            pass

    selected_raw = payload.get("selected_option_ids")
    selected: list[str] | None = None
    if isinstance(selected_raw, list) and selected_raw:
        selected = [str(s) for s in selected_raw]

    return TripIntent(
        origin=payload.get("origin") or None,
        destination=payload.get("destination") or None,
        departure_date=payload.get("departure_date") or None,
        return_date=payload.get("return_date") or None,
        travelers=travelers,
        currency=payload.get("currency") or None,
        purpose=payload.get("purpose") or None,
        include_flights=bool(payload.get("include_flights", True)),
        include_hotels=bool(payload.get("include_hotels", True)),
        conversation_id=payload.get("conversation_id") or None,
        traveler_id=payload.get("traveler_id") or None,
        selected_option_ids=selected,
        explain_with_llm=bool(payload.get("explain_with_llm", False)),
        constraints=dict(payload.get("constraints") or {}),
    )


_SLOT_QUESTIONS: dict[str, str] = {
    "origin": "What city or airport are you departing from? (e.g. SFO, London)",
    "destination": "What is your destination city or airport? (e.g. JFK, New York)",
    "departure_date": "What is your departure date? (e.g. 2026-06-10 or next Tuesday)",
    "travelers": "How many travelers are there?",
    "currency": "What currency should I use for pricing? (e.g. USD, EUR)",
}


def intent_slots(intent: TripIntent) -> dict[str, object]:
    """The user-facing slots of an intent (what the planner chips show).

    Returned to the client on every turn (output["intent"]) and stored on the
    assistant ConversationMessage trace so the next turn can merge onto it.
    """
    return {
        "origin": intent.origin,
        "destination": intent.destination,
        "departure_date": intent.departure_date,
        "return_date": intent.return_date,
        "travelers": intent.travelers,
        "currency": intent.currency,
        "purpose": intent.purpose,
    }


def missing_slot_question(intent: TripIntent) -> str | None:
    """Return exactly ONE clarifying question for the first missing required slot.

    Returns None when all required slots are present (no clarification needed).
    Checks slots in the canonical order defined by _REQUIRED_SLOTS so the
    question is deterministic: origin first, then destination, then date, etc.
    """
    for slot in _REQUIRED_SLOTS:
        if getattr(intent, slot) is None:
            return _SLOT_QUESTIONS[slot]
    return None
