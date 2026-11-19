"""UAT2 defect K: "Actually I'll come back a day later" moved the return EARLIER.

Browser evidence (employee, /planner, real Gemini): chips ORD->JFK depart
2026-11-18 return 2026-11-19, a draft trip holding a flight + hotel.  The
follow-up "Actually I'll come back a day later" set the return to 2026-11-18 (the
departure date, a 0-night stay), the planner re-searched flights only and the
trip was not updated.

Root cause: the deterministic duration regex read "a day later" as a 1-day TRIP
LENGTH ("for a day" -> return = departure + 0), and deterministic durations win
over the LLM's dates — so every provider produced the same-day return.

Pinned (hermetic — no DB, no network):
  * relative shifts of the return / departure date are resolved against the
    PRIOR intent ("a day later", "2 days later", "stay an extra night",
    "one more night", "leave a day earlier", ...), with the local provider AND
    with a stubbed LLM that returns the wrong absolute date (the shift wins);
  * a follow-up never produces return < departure, and never silently collapses
    a trip with nights into a same-day return — the prior return is kept and the
    planner asks — unless the user explicitly asked for a same-day return.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.agents.travel_agent.agent import TravelAgent
from ai_agents.agents.travel_agent.extraction import parse_message, resolve_slots
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse
from services.llm_provider.local import LocalLlmProvider

TODAY = date(2026, 11, 11)  # Wednesday

# The browser state: Wed 2026-11-18 -> Thu 2026-11-19 (one night).
PRIOR = {
    "origin": "ORD",
    "destination": "JFK",
    "departure_date": "2026-11-18",
    "return_date": "2026-11-19",
    "travelers": 1,
    "currency": "USD",
    "purpose": "Client meeting",
}
# A three-night trip, so "a day earlier" has room to move.
PRIOR_3N = {**PRIOR, "departure_date": "2026-11-17", "return_date": "2026-11-20"}


class _Scripted(LlmProvider):
    """A real-provider stand-in that returns one canned JSON reply."""

    model = "google/gemini-2.5-flash"

    def __init__(self, reply: dict[str, Any]) -> None:
        self.text = json.dumps(reply)
        self.calls = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text=self.text, model=self.model)

    def stream(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _wrong_llm(**override: Any) -> _Scripted:
    """What Gemini plausibly answered: a same-day return / a 1-day trip length."""
    reply: dict[str, Any] = {
        "origin": None,
        "destination": None,
        "departure_date": None,
        "return_date": "2026-11-18",
        "trip_length_days": 1,
        "nights": None,
        "travelers": None,
        "currency": None,
        "purpose": None,
    }
    reply.update(override)
    return _Scripted(reply)


def _providers() -> list[Any]:
    return [
        pytest.param(None, id="no-provider"),
        pytest.param(LocalLlmProvider(), id="local"),
        pytest.param("wrong-llm", id="stub-llm-wrong-date"),
    ]


def _resolve(text: str, prior: dict[str, Any], provider: Any, **llm: Any):
    if provider == "wrong-llm":
        provider = _wrong_llm(**llm)
    return resolve_slots({}, text=text, prior=prior, today=TODAY, provider=provider)


# ---------------------------------------------------------------------------
# Relative shifts against the prior intent
# ---------------------------------------------------------------------------

_RETURN_SHIFTS_1N = [
    # (message, expected return) against PRIOR (2026-11-18 -> 2026-11-19)
    ("Actually I'll come back a day later", "2026-11-20"),
    ("a day later", "2026-11-20"),
    ("one day later", "2026-11-20"),
    ("come back one day later please", "2026-11-20"),
    ("2 days later", "2026-11-21"),
    ("Return two days later", "2026-11-21"),
    ("stay an extra night", "2026-11-20"),
    ("stay an extra day", "2026-11-20"),
    ("I'd like one more night", "2026-11-20"),
    ("Let's add another night", "2026-11-20"),
    ("stay 2 extra nights", "2026-11-21"),
    ("come back Friday", "2026-11-20"),
]


class TestRelativeReturnShift:
    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(("text", "expected"), _RETURN_SHIFTS_1N)
    def test_return_shift_against_prior(self, text: str, expected: str, provider: Any) -> None:
        r = _resolve(text, PRIOR, provider)
        assert r.slots["return_date"] == expected
        assert r.slots["departure_date"] == "2026-11-18"  # untouched
        assert r.slots["origin"] == "ORD" and r.slots["destination"] == "JFK"
        assert r.slots["purpose"] == "Client meeting"
        assert r.notice is None
        assert not any(k.startswith("_") for k in r.slots)
        assert not any(k.startswith("_") for k in r.extracted)

    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Actually come back a day earlier", "2026-11-19"),
            ("a day earlier", "2026-11-19"),
            ("one night less", "2026-11-19"),
            ("one less night", "2026-11-19"),
        ],
    )
    def test_return_earlier_against_three_night_prior(
        self, text: str, expected: str, provider: Any
    ) -> None:
        r = _resolve(text, PRIOR_3N, provider, return_date="2026-11-17")
        assert r.slots["return_date"] == expected
        assert r.slots["departure_date"] == "2026-11-17"
        assert r.notice is None

    def test_shift_applies_to_the_chips_value_over_the_prior(self) -> None:
        # The chips the browser sends are the current state; the shift is relative to them.
        r = resolve_slots(
            {"return_date": "2026-11-22"},
            text="come back a day later",
            prior=PRIOR,
            today=TODAY,
        )
        assert r.slots["return_date"] == "2026-11-23"

    def test_a_day_later_is_not_a_trip_length(self) -> None:
        out = parse_message("Actually I'll come back a day later", today=TODAY)
        assert "_duration" not in out
        assert "return_date" not in out  # no absolute date was stated

    def test_trip_length_still_parses(self) -> None:
        assert parse_message("for 3 days", today=TODAY)["_duration"] == (3, "day")
        r = resolve_slots({}, text="make it 2 nights", prior=PRIOR, today=TODAY)
        assert r.slots["return_date"] == "2026-11-20"

    def test_shift_without_a_known_return_counts_from_departure(self) -> None:
        prior = {**PRIOR, "return_date": None}
        r = resolve_slots({}, text="come back a day later", prior=prior, today=TODAY)
        assert r.slots["return_date"] == "2026-11-19"
        assert r.slots["departure_date"] == "2026-11-18"

    def test_earlier_without_a_known_return_is_ignored(self) -> None:
        prior = {**PRIOR, "return_date": None}
        r = resolve_slots({}, text="come back a day earlier", prior=prior, today=TODAY)
        assert r.slots["return_date"] is None

    def test_back_a_week_later_in_a_first_message(self) -> None:
        r = resolve_slots(
            {"origin": "ORD", "destination": "JFK"},
            text="fly out next Tuesday and come back a week later",
            prior=None,
            today=TODAY,
        )
        assert r.slots["departure_date"] == "2026-11-17"
        assert r.slots["return_date"] == "2026-11-24"

    def test_return_shift_counts_from_a_new_departure_in_the_same_message(self) -> None:
        r = resolve_slots(
            {}, text="leave Dec 10 and come back two days later", prior=PRIOR, today=TODAY
        )
        assert r.slots["departure_date"] == "2026-12-10"
        assert r.slots["return_date"] == "2026-12-12"

    def test_llm_wrong_departure_is_ignored_when_a_shift_was_stated(self) -> None:
        provider = _wrong_llm(departure_date="2026-11-19", return_date="2026-11-19")
        r = resolve_slots(
            {},
            text="Actually I'll come back a day later",
            prior=PRIOR,
            today=TODAY,
            provider=provider,
        )
        assert provider.calls == 1
        assert r.source == "llm"
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-11-20"


class TestRelativeDepartureShift:
    @pytest.mark.parametrize("provider", _providers())
    def test_leave_a_day_earlier_keeps_the_return(self, provider: Any) -> None:
        r = _resolve(
            "leave a day earlier", PRIOR, provider, departure_date="2026-11-18", return_date=None
        )
        assert r.slots["departure_date"] == "2026-11-17"
        assert r.slots["return_date"] == "2026-11-19"  # the stay grows, not dropped
        assert r.notice is None

    @pytest.mark.parametrize("provider", _providers())
    def test_leave_a_day_later_against_three_night_prior(self, provider: Any) -> None:
        r = _resolve("I'll fly out a day later", PRIOR_3N, provider, departure_date="2026-11-17")
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-11-20"

    def test_leave_and_return_in_one_message(self) -> None:
        r = resolve_slots(
            {},
            text="leave a day earlier and come back 2 days later",
            prior=PRIOR,
            today=TODAY,
        )
        assert r.slots["departure_date"] == "2026-11-17"
        assert r.slots["return_date"] == "2026-11-21"


# ---------------------------------------------------------------------------
# Validation: no silent collapse to a same-day / backwards trip
# ---------------------------------------------------------------------------


class TestFollowUpDateValidation:
    @pytest.mark.parametrize("provider", _providers())
    def test_a_day_earlier_on_one_night_trip_keeps_prior_and_asks(self, provider: Any) -> None:
        r = _resolve("come back a day earlier", PRIOR, provider)
        assert r.slots["return_date"] == "2026-11-19"  # prior kept
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.notice is not None
        assert "2026-11-18" in r.notice and "2026-11-19" in r.notice
        assert "same day" in r.notice

    def test_llm_same_day_return_on_a_trip_with_nights_is_rejected(self) -> None:
        # No relative phrase the parser knows: the LLM's absolute date is checked.
        r = resolve_slots(
            {},
            text="hmm, change the way back",
            prior=PRIOR,
            today=TODAY,
            provider=_wrong_llm(trip_length_days=None),
        )
        assert r.slots["return_date"] == "2026-11-19"
        assert r.notice is not None

    def test_llm_return_before_departure_keeps_prior(self) -> None:
        r = resolve_slots(
            {},
            text="hmm, change the way back",
            prior=PRIOR,
            today=TODAY,
            provider=_wrong_llm(return_date="2026-11-16", trip_length_days=None),
        )
        assert r.slots["return_date"] == "2026-11-19"
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.notice is not None

    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        "text",
        ["Actually make it a day trip", "I'll come back the same day", "same-day return please"],
    )
    def test_explicit_same_day_is_allowed(self, text: str, provider: Any) -> None:
        r = _resolve(text, PRIOR, provider, trip_length_days=None)
        assert r.slots["return_date"] == "2026-11-18"
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.notice is None

    def test_same_day_allowed_when_prior_had_no_nights(self) -> None:
        prior = {**PRIOR, "return_date": None}
        r = resolve_slots(
            {},
            text="change it",
            prior=prior,
            today=TODAY,
            provider=_wrong_llm(trip_length_days=None),
        )
        assert r.slots["return_date"] == "2026-11-18"
        assert r.notice is None

    def test_chip_edit_to_same_day_is_not_second_guessed(self) -> None:
        # A manual chip edit is an explicit choice (no message text).
        r = resolve_slots({"return_date": "2026-11-18"}, text=None, prior=PRIOR, today=TODAY)
        assert r.slots["return_date"] == "2026-11-18"
        assert r.notice is None


# ---------------------------------------------------------------------------
# "N-day trip" is a trip LENGTH, never an explicit same-day return
# (review regression: the "day trip" phrase matched inside "3-day trip")
# ---------------------------------------------------------------------------

_NO_PRIOR_DURATIONS = [
    # (message, expected departure, expected return) with no prior intent
    ("Plan a 3-day trip from ORD to JFK on Nov 18", "2026-11-18", "2026-11-20"),
    ("ORD to JFK Nov 18 for a five day trip", "2026-11-18", "2026-11-22"),
    ("I need a 2-day trip Chicago to New York departing Nov 18", "2026-11-18", "2026-11-19"),
    ("ORD to JFK on Nov 18, 5 day trip", "2026-11-18", "2026-11-22"),
    ("ORD to JFK on Nov 18, a four-day trip", "2026-11-18", "2026-11-21"),
]


class TestCountedDayTripIsALength:
    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(("text", "dep", "ret"), _NO_PRIOR_DURATIONS)
    def test_n_day_trip_without_prior(self, text: str, dep: str, ret: str, provider: Any) -> None:
        r = _resolve(text, {}, provider)
        assert "_same_day" not in parse_message(text, today=TODAY)
        assert r.slots["departure_date"] == dep
        assert r.slots["return_date"] == ret
        assert r.notice is None

    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        ("text", "ret"),
        [
            ("make it a 3-day trip", "2026-11-20"),
            ("Make it a 3 day trip", "2026-11-20"),
            ("let's do a five day trip instead", "2026-11-22"),
        ],
    )
    def test_make_it_n_day_trip_against_prior(self, text: str, ret: str, provider: Any) -> None:
        r = _resolve(text, PRIOR, provider)
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == ret
        assert r.notice is None

    @pytest.mark.parametrize("text", ["a multi-day trip", "a few day trip", "a couple day trip"])
    def test_quantified_day_trip_is_not_same_day(self, text: str) -> None:
        assert "_same_day" not in parse_message(text, today=TODAY)

    @pytest.mark.parametrize(
        "text", ["Actually make it a day trip", "a one-day trip please", "day trip to JFK"]
    )
    def test_plain_day_trip_still_same_day(self, text: str) -> None:
        assert parse_message(text, today=TODAY).get("_same_day") is True


class TestNegatedSameDay:
    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        "text",
        [
            "I don't want to come back the same day",
            "I do not want to return the same day",
            "it's not a day trip",
            "this isn't a day trip",
            "no same-day return please",
            "I won't be back the same day",
        ],
    )
    def test_negated_same_day_never_collapses(self, text: str, provider: Any) -> None:
        assert "_same_day" not in parse_message(text, today=TODAY)
        r = _resolve(text, PRIOR, provider)
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-11-19"  # never the same-day 2026-11-18

    def test_negation_in_an_earlier_clause_does_not_cancel(self) -> None:
        text = "I can't stay overnight, so make it a day trip"
        assert parse_message(text, today=TODAY).get("_same_day") is True


# ---------------------------------------------------------------------------
# TravelAgent end to end (mock inventory, patched policy eval + decision log)
# ---------------------------------------------------------------------------

_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.llm,
    task_type="trip_planning",
    confidence=0.95,
    rationale="context_type=travel_planning",
)


def _all_in_policy(items: Any, **_kw: Any) -> tuple[Any, ...]:
    return tuple(MagicMock(in_policy=True, verdicts=()) for _ in items)


def _run(agent: TravelAgent, payload: dict[str, Any]) -> tuple[Any, MagicMock]:
    ctx = AgentContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="uat2-k",
    )
    with (
        patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()
        ) as log,
        patch(
            "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
            side_effect=_all_in_policy,
        ),
    ):
        result = agent.handle(
            Task(context_type="travel_planning", payload=payload),
            _DECISION,
            context=ctx,
            session=MagicMock(),
        )
    return result, log


class TestTravelAgentFollowUp:
    @pytest.mark.parametrize(
        "provider", [LocalLlmProvider(), _wrong_llm()], ids=["local", "stub-llm-wrong-date"]
    )
    def test_a_day_later_researches_flights_and_hotels(self, provider: LlmProvider) -> None:
        agent = TravelAgent(provider=provider, today=lambda: TODAY)
        result, _ = _run(agent, {**PRIOR, "question": "Actually I'll come back a day later"})
        assert result.output["clarify"] is False
        assert result.output["intent"]["return_date"] == "2026-11-20"
        assert result.output["intent"]["departure_date"] == "2026-11-18"
        segs = {o["segment_type"] for o in result.output["shortlist"]}
        assert segs == {"flight", "hotel"}

    def test_collapse_to_same_day_asks_instead_of_searching(self) -> None:
        agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)
        result, log = _run(agent, {**PRIOR, "question": "come back a day earlier"})
        assert result.output["clarify"] is True
        assert "same day" in result.output["question"]
        assert result.output["intent"]["return_date"] == "2026-11-19"  # chips unchanged
        assert log.call_count == 1

    @pytest.mark.parametrize(
        "provider", [LocalLlmProvider(), _wrong_llm()], ids=["local", "stub-llm-wrong-date"]
    )
    def test_n_day_trip_searches_hotels(self, provider: LlmProvider) -> None:
        agent = TravelAgent(provider=provider, today=lambda: TODAY)
        result, _ = _run(agent, {"question": "Plan a 3-day trip from ORD to JFK on Nov 18"})
        assert result.output["clarify"] is False
        assert result.output["intent"]["departure_date"] == "2026-11-18"
        assert result.output["intent"]["return_date"] == "2026-11-20"
        segs = {o["segment_type"] for o in result.output["shortlist"]}
        assert segs == {"flight", "hotel"}


class TestExplicitDateBeatsShift:
    """A date written out in the same message wins over an unrelated relative phrase."""

    @pytest.mark.parametrize("provider", _providers())
    def test_explicit_return_kept(self, provider: Any) -> None:
        text = "Book ORD to JFK Nov 18 to Nov 20, my meeting starts a day later"
        r = _resolve(text, {}, provider)
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-11-20"

    @pytest.mark.parametrize("provider", _providers())
    def test_shift_still_applies_without_explicit_return(self, provider: Any) -> None:
        r = _resolve("Actually I'll come back a day later", PRIOR, provider)
        assert r.slots["return_date"] == "2026-11-20"
