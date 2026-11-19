"""Product gap 8 (fxcard track): "a few days later", "several", "a fortnight", "next week".

The deterministic parser knew a / one..ten / couple of, so "come back a few days
later" left the dates unchanged without the LLM.  Pinned (hermetic — no DB, no
network), with no provider, the local provider and a stub LLM that answers a
WRONG absolute date (the pattern of test_uat2_relative_date_shift.py):

  * "a few" = 3 in relative shifts ("a few days later", "a few more nights",
    "extend by a few days"); the deterministic shift beats the LLM's date, and an
    explicit date in the same message beats the shift;
  * "several" is NOT guessed: the prior dates are kept and the planner asks how
    many days through the existing notice mechanism (the LLM's guess is dropped);
  * "a week later" was already handled (pinned here, not re-implemented);
  * "a fortnight" = 14 days (shift, "in a fortnight", trip length);
  * a bare "next week" (no weekday) is not guessed either: the planner asks which day.
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

PRIOR = {
    "origin": "ORD",
    "destination": "JFK",
    "departure_date": "2026-11-18",
    "return_date": "2026-11-19",
    "travelers": 1,
    "currency": "USD",
    "purpose": "Client meeting",
}
PRIOR_5N = {**PRIOR, "departure_date": "2026-11-16", "return_date": "2026-11-21"}


class _Scripted(LlmProvider):
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
    """A model that guesses: a wrong return date and a wrong departure."""
    reply: dict[str, Any] = {
        "origin": None,
        "destination": None,
        "departure_date": "2026-11-25",
        "return_date": "2026-11-26",
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


def _resolve(text: str, prior: dict[str, Any] | None, provider: Any, **llm: Any):
    if provider == "wrong-llm":
        provider = _wrong_llm(**llm)
    return resolve_slots({}, text=text, prior=prior, today=TODAY, provider=provider)


# ---------------------------------------------------------------------------
# "a few" = 3
# ---------------------------------------------------------------------------


class TestAFew:
    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Actually I'll come back a few days later", "2026-11-22"),
            ("a few days later", "2026-11-22"),
            ("stay a few more nights", "2026-11-22"),
            ("add a few extra days", "2026-11-22"),
            ("extend the trip by a few days", "2026-11-22"),
        ],
    )
    def test_a_few_days_later_moves_the_return_by_three(
        self, text: str, expected: str, provider: Any
    ) -> None:
        r = _resolve(text, PRIOR, provider)
        assert r.slots["return_date"] == expected
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.notice is None

    @pytest.mark.parametrize("provider", _providers())
    def test_leave_a_few_days_earlier(self, provider: Any) -> None:
        r = _resolve("leave a few days earlier", PRIOR_5N, provider)
        assert r.slots["departure_date"] == "2026-11-13"
        assert r.slots["return_date"] == "2026-11-21"  # the stay just grows

    @pytest.mark.parametrize("provider", _providers())
    def test_a_few_days_shorter(self, provider: Any) -> None:
        r = _resolve("come back a few days earlier", PRIOR_5N, provider)
        assert r.slots["return_date"] == "2026-11-18"

    def test_a_few_is_parsed_as_a_shift_not_a_trip_length(self) -> None:
        out = parse_message("come back a few days later", today=TODAY)
        assert out["_shift"] == {"return_date": 3}
        assert "_duration" not in out
        assert "return_date" not in out

    @pytest.mark.parametrize("provider", _providers())
    def test_explicit_date_in_the_same_message_beats_a_few(self, provider: Any) -> None:
        r = _resolve("back on Nov 24, a few days later", PRIOR, provider)
        assert r.slots["return_date"] == "2026-11-24"

    def test_a_few_never_sets_the_traveler_count(self) -> None:
        # Scope: only date shifts learn "a few" — a headcount is never guessed.
        out = parse_message("a few of us fly to JFK", today=TODAY)
        assert "travelers" not in out


# ---------------------------------------------------------------------------
# "several" -> ask, never guess
# ---------------------------------------------------------------------------


class TestSeveral:
    @pytest.mark.parametrize("provider", _providers())
    @pytest.mark.parametrize(
        "text",
        [
            "come back several days later",
            "Actually I'll stay several more nights",
            "leave several days earlier",
            "extend it by several days",
        ],
    )
    def test_several_keeps_the_dates_and_asks(self, text: str, provider: Any) -> None:
        r = _resolve(text, PRIOR, provider)
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-11-19"
        assert r.notice is not None
        assert "How many days" in r.notice
        assert "several" in r.notice.lower()
        assert "departure_date" not in r.extracted and "return_date" not in r.extracted

    def test_several_is_flagged_by_the_parser(self) -> None:
        out = parse_message("come back several days later", today=TODAY)
        assert out.get("_vague") == "several"
        assert "_shift" not in out and "_duration" not in out

    @pytest.mark.parametrize("provider", _providers())
    def test_explicit_dates_in_the_same_message_win_over_several(self, provider: Any) -> None:
        r = _resolve("Nov 20 to Nov 24, several days in total", PRIOR, provider)
        assert r.slots["departure_date"] == "2026-11-20"
        assert r.slots["return_date"] == "2026-11-24"
        assert r.notice is None

    def test_several_without_a_time_unit_is_not_a_date_question(self) -> None:
        out = parse_message("I have several meetings in New York", today=TODAY)
        assert "_vague" not in out


# ---------------------------------------------------------------------------
# "a week later" (already handled), "a fortnight", "next week"
# ---------------------------------------------------------------------------


class TestWeeksAndFortnights:
    @pytest.mark.parametrize("provider", _providers())
    def test_a_week_later_was_already_handled(self, provider: Any) -> None:
        r = _resolve("come back a week later", PRIOR, provider)
        assert r.slots["return_date"] == "2026-11-26"

    @pytest.mark.parametrize("provider", _providers())
    def test_a_fortnight_later(self, provider: Any) -> None:
        r = _resolve("come back a fortnight later", PRIOR, provider)
        assert r.slots["return_date"] == "2026-12-03"
        assert r.notice is None

    def test_in_a_fortnight_is_a_departure_date(self) -> None:
        out = parse_message("fly to JFK in a fortnight", today=TODAY)
        assert out["departure_date"] == "2026-11-25"

    def test_for_a_fortnight_is_a_trip_length(self) -> None:
        r = resolve_slots(
            {"origin": "ORD", "destination": "JFK"},
            text="leave Nov 18 for a fortnight",
            prior=None,
            today=TODAY,
        )
        assert r.slots["departure_date"] == "2026-11-18"
        assert r.slots["return_date"] == "2026-12-02"

    @pytest.mark.parametrize("provider", _providers())
    def test_bare_next_week_asks_which_day(self, provider: Any) -> None:
        r = _resolve(
            "I need to fly from ORD to JFK next week",
            {"travelers": 1},
            provider,
            departure_date="2026-11-17",
            return_date=None,
        )
        assert r.slots["departure_date"] is None
        assert r.notice is not None and "Which day next week" in r.notice

    def test_next_weekday_is_not_vague(self) -> None:
        out = parse_message("fly out next Tuesday", today=TODAY)
        assert out["departure_date"] == "2026-11-17"
        assert "_vague" not in out

    def test_next_weekend_is_not_next_week(self) -> None:
        assert "_vague" not in parse_message("fly out next weekend", today=TODAY)


# ---------------------------------------------------------------------------
# Agent: the notice becomes the clarify question (no search)
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


def _run(agent: TravelAgent, payload: dict[str, Any]) -> Any:
    ctx = AgentContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="pg8-vague",
    )
    with (
        patch("ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()),
        patch(
            "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
            side_effect=_all_in_policy,
        ),
    ):
        return agent.handle(
            Task(context_type="travel_planning", payload=payload),
            _DECISION,
            context=ctx,
            session=MagicMock(),
        )


class TestTravelAgent:
    @pytest.mark.parametrize(
        "provider", [LocalLlmProvider(), _wrong_llm()], ids=["local", "stub-llm-wrong-date"]
    )
    def test_a_few_days_later_researches_with_the_new_return(self, provider: LlmProvider) -> None:
        agent = TravelAgent(provider=provider, today=lambda: TODAY)
        result = _run(agent, {**PRIOR, "question": "Actually I'll come back a few days later"})
        assert result.output["clarify"] is False
        assert result.output["intent"]["return_date"] == "2026-11-22"

    @pytest.mark.parametrize(
        "provider", [LocalLlmProvider(), _wrong_llm()], ids=["local", "stub-llm-wrong-date"]
    )
    def test_several_days_later_asks_instead_of_searching(self, provider: LlmProvider) -> None:
        agent = TravelAgent(provider=provider, today=lambda: TODAY)
        result = _run(agent, {**PRIOR, "question": "come back several days later"})
        assert result.output["clarify"] is True
        assert "How many days" in result.output["question"]
        assert result.output["intent"]["return_date"] == "2026-11-19"
