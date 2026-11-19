"""Planner free-text intent extraction + cross-turn merge (C7R planner defect).

UAT defect: with real Gemini, EVERY planner message ("Fly from Chicago to New York
next Tuesday for 3 days, ...", a reply "Chicago (ORD)", a fully explicit request)
got the same "What city or airport are you departing from?" clarify — the
TravelAgent never read the message text at all (payload["question"] was ignored),
so no slot was ever filled from prose.

These tests pin the fix, all hermetic (no DB, no network):
  * deterministic parsing (relative dates against an injected today, return date
    from "N days"/"N nights", locations, purpose, travelers, currency);
  * tolerant parsing of representative Gemini-shaped LLM outputs + fallback;
  * merge order prior turn < chips < message text, and the clarify answer path;
  * TravelAgent end-to-end over the mock inventory (no-LLM path and LLM path).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.agents.travel_agent.agent import TravelAgent
from ai_agents.agents.travel_agent.extraction import (
    parse_llm_json,
    parse_message,
    resolve_slots,
    return_from_duration,
)
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse
from services.llm_provider.local import LocalLlmProvider

# Wednesday — "next Tuesday" is 2026-11-17, "next Thursday" 2026-11-19.
TODAY = date(2026, 11, 11)

UAT_MESSAGE = (
    "Fly from Chicago to New York next Tuesday for 3 days, client meeting, "
    "need a hotel near Midtown"
)
EXPLICIT_MESSAGE = (
    "Fly from San Francisco to New York on 2026-06-10, returning 2026-06-14 " "for a client meeting"
)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _Scripted(LlmProvider):
    """Returns one canned completion; records the prompts it saw."""

    model = "google/gemini-2.5-flash"

    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> LlmResponse:
        self.prompts.append(messages[-1].content)
        return LlmResponse(text=self.text, model=self.model)

    def stream(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> Iterator[LlmChunk]:
        yield from []


class _Failing(_Scripted):
    def __init__(self) -> None:
        super().__init__("")

    def complete(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> LlmResponse:
        raise ProviderError("upstream 503", code="PROVIDER_UNAVAILABLE")


class _MustNotCall(_Scripted):
    def __init__(self) -> None:
        super().__init__("")

    def complete(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> LlmResponse:
        raise AssertionError("LLM must not be called on this path")


# Representative Gemini 2.5 Flash replies (captured shapes from OpenRouter).
GEMINI_UAT = (
    '{"origin": "Chicago (ORD)", "destination": "New York (NYC)", '
    '"departure_date": "2026-11-17", "return_date": null, "trip_length_days": 3, '
    '"nights": null, "travelers": null, "currency": null, "purpose": "Client meeting"}'
)
GEMINI_FENCED = (
    "```json\n"
    '{"origin": "San Francisco (SFO)", "destination": "New York (NYC)", '
    '"departure_date": "2026-06-10", "return_date": "2026-06-14", '
    '"trip_length_days": null, "nights": null, "travelers": null, '
    '"currency": null, "purpose": "client meeting"}\n'
    "```"
)


# ---------------------------------------------------------------------------
# Deterministic parser
# ---------------------------------------------------------------------------


class TestDeterministicParser:
    def test_uat_sentence_fills_route_dates_and_purpose(self) -> None:
        slots = parse_message(UAT_MESSAGE, today=TODAY)
        assert slots["origin"] == "Chicago"
        assert slots["destination"] == "New York"
        assert slots["departure_date"] == "2026-11-17"
        assert slots["_duration"] == (3, "day")
        assert slots["purpose"] == "Client meeting"
        # "near Midtown" is a hotel neighbourhood, not a trip leg.
        assert "Midtown" not in str(slots.get("destination"))

    def test_explicit_iso_dates_with_return_cue(self) -> None:
        slots = parse_message(EXPLICIT_MESSAGE, today=TODAY)
        assert slots["origin"] == "San Francisco"
        assert slots["destination"] == "New York"
        assert slots["departure_date"] == "2026-06-10"
        assert slots["return_date"] == "2026-06-14"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("leaving next Tuesday", "2026-11-17"),
            ("leaving next Thursday", "2026-11-19"),
            ("on Friday", "2026-11-13"),
            ("this Monday", "2026-11-16"),
            ("tomorrow", "2026-11-12"),
            ("day after tomorrow", "2026-11-13"),
            ("in 2 weeks", "2026-11-25"),
            ("on Dec 3", "2026-12-03"),
            ("on 3rd December", "2026-12-03"),
            ("on June 10", "2027-06-10"),  # past month/day rolls to next year
            ("on 12/1", "2026-12-01"),
            ("on 2026-06-10", "2026-06-10"),  # explicit past date kept as typed
        ],
    )
    def test_relative_and_absolute_dates_resolve_against_today(
        self, text: str, expected: str
    ) -> None:
        assert parse_message(text, today=TODAY)["departure_date"] == expected

    def test_month_day_range(self) -> None:
        slots = parse_message("SFO to JFK Dec 3-6", today=TODAY)
        assert slots["departure_date"] == "2026-12-03"
        assert slots["return_date"] == "2026-12-06"
        assert slots["origin"] == "SFO"
        assert slots["destination"] == "JFK"

    def test_back_on_is_a_return_date(self) -> None:
        slots = parse_message("depart Dec 3, back on Dec 7", today=TODAY)
        assert slots["departure_date"] == "2026-12-03"
        assert slots["return_date"] == "2026-12-07"

    def test_bare_answer_fills_the_pending_slot(self) -> None:
        assert parse_message("Chicago (ORD)", today=TODAY, pending_slot="origin") == {
            "origin": "Chicago (ORD)"
        }
        assert parse_message("2", today=TODAY, pending_slot="travelers") == {"travelers": 2}
        assert parse_message("EUR", today=TODAY, pending_slot="currency") == {"currency": "EUR"}

    def test_travelers_and_currency(self) -> None:
        slots = parse_message("two travelers, prices in EUR", today=TODAY)
        assert slots["travelers"] == 2
        assert slots["currency"] == "EUR"
        assert parse_message("just me", today=TODAY)["travelers"] == 1

    def test_unknown_city_is_captured_raw(self) -> None:
        slots = parse_message("from Boston to Austin tomorrow", today=TODAY)
        assert slots["origin"] == "Boston"
        assert slots["destination"] == "Austin"

    def test_in_n_days_is_a_date_not_a_trip_length(self) -> None:
        slots = parse_message("fly to London in 3 days", today=TODAY)
        assert slots["departure_date"] == "2026-11-14"
        assert "_duration" not in slots

    def test_lowercase_code_like_words_are_not_airports(self) -> None:
        assert "origin" not in parse_message("the word ord appears here", today=TODAY)

    @pytest.mark.parametrize(
        ("count", "unit", "expected"),
        [(3, "day", "2026-11-19"), (2, "night", "2026-11-19"), (1, "week", "2026-11-24")],
    )
    def test_return_from_duration(self, count: int, unit: str, expected: str) -> None:
        assert return_from_duration("2026-11-17", count, unit) == expected


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------


class TestLlmJsonParsing:
    def test_plain_gemini_object(self) -> None:
        data = parse_llm_json(GEMINI_UAT)
        assert data == {
            "origin": "Chicago (ORD)",
            "destination": "New York (NYC)",
            "departure_date": "2026-11-17",
            "trip_length_days": 3,
            "purpose": "Client meeting",
        }

    def test_markdown_fence_and_prose(self) -> None:
        data = parse_llm_json("Sure! Here you go:\n" + GEMINI_FENCED + "\nHope it helps.")
        assert data is not None
        assert data["return_date"] == "2026-06-14"

    def test_wrapped_object_alias_keys_and_null_strings(self) -> None:
        raw = (
            '{"intent": {"from": "Chicago", "to": "NYC", "depart_date": "next Tuesday", '
            '"duration_days": "3", "passengers": "null", "currency": "unknown"}}'
        )
        data = parse_llm_json(raw)
        assert data == {
            "origin": "Chicago",
            "destination": "NYC",
            "departure_date": "next Tuesday",
            "trip_length_days": "3",
        }

    @pytest.mark.parametrize("raw", ["", "I could not find a trip.", "[1, 2]", "{not json}"])
    def test_garbage_returns_none(self, raw: str) -> None:
        assert parse_llm_json(raw) is None


# ---------------------------------------------------------------------------
# Merge (prior < chips < text) + LLM / fallback selection
# ---------------------------------------------------------------------------


class TestResolveSlots:
    def test_uat_sentence_deterministic_only(self) -> None:
        r = resolve_slots({"currency": "USD"}, text=UAT_MESSAGE, prior=None, today=TODAY)
        assert r.slots == {
            "origin": "ORD",
            "destination": "JFK",
            "departure_date": "2026-11-17",
            "return_date": "2026-11-19",  # 3 days: Tue, Wed, Thu
            "travelers": 1,
            "currency": "USD",
            "purpose": "Client meeting",
        }
        assert r.source == "deterministic"

    def test_gemini_output_is_used_and_normalised(self) -> None:
        provider = _Scripted(GEMINI_UAT)
        r = resolve_slots({}, text=UAT_MESSAGE, prior=None, today=TODAY, provider=provider)
        assert r.source == "llm"
        assert r.slots["origin"] == "ORD"  # "Chicago (ORD)" -> code
        assert r.slots["destination"] == "JFK"  # "New York (NYC)" -> served airport
        assert r.slots["return_date"] == "2026-11-19"  # computed, not model math
        assert "2026-11-11" in provider.prompts[0]  # today is given to the model

    def test_deterministic_dates_beat_llm_dates(self) -> None:
        wrong_year = GEMINI_UAT.replace("2026-11-17", "2025-11-18")
        r = resolve_slots(
            {}, text=UAT_MESSAGE, prior=None, today=TODAY, provider=_Scripted(wrong_year)
        )
        assert r.slots["departure_date"] == "2026-11-17"

    def test_llm_fills_what_the_parser_cannot(self) -> None:
        text = "I'm off to see a customer in the Big Apple, leaving from the Windy City"
        reply = '{"origin": "Chicago (ORD)", "destination": "New York (JFK)"}'
        r = resolve_slots({}, text=text, prior=None, today=TODAY, provider=_Scripted(reply))
        assert (r.slots["origin"], r.slots["destination"]) == ("ORD", "JFK")

    @pytest.mark.parametrize(
        "provider", [_Failing(), _Scripted("Sorry, I can't help with that."), _Scripted("{}")]
    )
    def test_llm_failure_falls_back_to_parser(self, provider: LlmProvider) -> None:
        r = resolve_slots({}, text=EXPLICIT_MESSAGE, prior=None, today=TODAY, provider=provider)
        assert r.slots["origin"] == "SFO"
        assert r.slots["destination"] == "JFK"
        assert r.slots["return_date"] == "2026-06-14"

    def test_local_stub_provider_is_never_called(self) -> None:
        class _CountingLocal(LocalLlmProvider):
            calls = 0

            def complete(self, messages: list[LlmMessage], **kw: Any) -> LlmResponse:
                type(self).calls += 1
                return super().complete(messages, **kw)

        resolve_slots({}, text=UAT_MESSAGE, prior=None, today=TODAY, provider=_CountingLocal())
        assert _CountingLocal.calls == 0

    def test_clarify_answer_merges_onto_prior_turn(self) -> None:
        prior = {
            "origin": None,
            "destination": "JFK",
            "departure_date": "2026-11-17",
            "return_date": "2026-11-19",
            "travelers": 1,
            "currency": "USD",
            "purpose": "Client meeting",
        }
        r = resolve_slots({}, text="Chicago (ORD)", prior=prior, today=TODAY)
        assert r.slots == {**prior, "origin": "ORD"}

    def test_text_beats_chips_and_chips_beat_prior(self) -> None:
        prior = {"origin": "SFO", "destination": "LHR", "departure_date": "2026-12-01"}
        chips = {"destination": "JFK", "travelers": 2}
        r = resolve_slots(chips, text="actually from Chicago", prior=prior, today=TODAY)
        assert r.slots["origin"] == "ORD"  # text
        assert r.slots["destination"] == "JFK"  # chips over prior
        assert r.slots["departure_date"] == "2026-12-01"  # prior kept
        assert r.slots["travelers"] == 2

    def test_explicit_null_chip_clears_optional_prior_slot(self) -> None:
        prior = {"origin": "SFO", "return_date": "2026-12-05", "purpose": "Offsite"}
        r = resolve_slots(
            {"return_date": None, "purpose": None}, text=None, prior=prior, today=TODAY
        )
        assert r.slots["return_date"] is None
        assert r.slots["purpose"] is None
        assert r.slots["origin"] == "SFO"

    def test_new_departure_drops_stale_return(self) -> None:
        prior = {"departure_date": "2026-12-01", "return_date": "2026-12-03"}
        r = resolve_slots({}, text="change it to Dec 10", prior=prior, today=TODAY)
        assert r.slots["departure_date"] == "2026-12-10"
        assert r.slots["return_date"] is None

    def test_return_before_departure_is_dropped(self) -> None:
        r = resolve_slots(
            {"departure_date": "2026-12-10", "return_date": "2026-12-01"},
            text=None,
            prior={"origin": "SFO"},
            today=TODAY,
        )
        assert r.slots["return_date"] is None


# ---------------------------------------------------------------------------
# TravelAgent end-to-end (mock inventory, patched policy eval + decision log)
# ---------------------------------------------------------------------------

_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.llm,
    task_type="trip_planning",
    confidence=0.95,
    rationale="context_type=travel_planning",
)


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="test",
    )


def _all_in_policy(items: Any, **_kw: Any) -> tuple[Any, ...]:
    return tuple(MagicMock(in_policy=True, verdicts=()) for _ in items)


def _run(agent: TravelAgent, payload: dict[str, Any]) -> tuple[Any, MagicMock]:
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
            context=_ctx(),
            session=MagicMock(),
        )
    return result, log


class TestTravelAgentReadsMessageText:
    def test_uat_message_plans_instead_of_asking_for_origin(self) -> None:
        agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)
        result, _ = _run(agent, {"question": UAT_MESSAGE, "currency": "USD"})

        assert result.status == AgentResultStatus.ok
        assert result.output["clarify"] is False
        assert result.output["intent"]["origin"] == "ORD"
        assert result.output["intent"]["return_date"] == "2026-11-19"
        shortlist = result.output["shortlist"]
        segs = {o["segment_type"] for o in shortlist}
        assert segs == {"flight", "hotel"}  # hotels searched for the stay
        assert all(
            "ORDJFK-2026-11-17" in o["option_id"]
            for o in shortlist
            if o["segment_type"] == "flight"
        )

    def test_partial_message_asks_once_and_returns_partial_intent(self) -> None:
        agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)
        result, log = _run(agent, {"question": "Fly to New York next Tuesday for 3 days"})
        assert result.output["clarify"] is True
        assert "departing from" in result.output["question"]
        assert result.output["intent"]["destination"] == "JFK"
        assert result.output["intent"]["departure_date"] == "2026-11-17"
        assert log.call_count == 1

    def test_llm_path_marks_route_llm_and_survives_provider_errors(self) -> None:
        agent = TravelAgent(provider=_Scripted(GEMINI_UAT), today=lambda: TODAY)
        result, log = _run(agent, {"question": UAT_MESSAGE})
        assert result.output["clarify"] is False
        record = log.call_args.kwargs["record"]
        assert record.route == AiRoute.llm
        assert record.input_summary["extraction_source"] == "llm"

        agent = TravelAgent(provider=_Failing(), today=lambda: TODAY)
        result, log = _run(agent, {"question": UAT_MESSAGE})
        assert result.output["clarify"] is False  # deterministic fallback
        assert log.call_args.kwargs["record"].route == AiRoute.deterministic

    def test_selection_and_chip_turns_never_call_the_llm(self) -> None:
        agent = TravelAgent(provider=_MustNotCall(), today=lambda: TODAY)
        base = {
            "origin": "SFO",
            "destination": "JFK",
            "departure_date": "2026-06-10",
            "travelers": 1,
            "currency": "USD",
            "include_hotels": False,
        }
        _run(agent, {**base, "question": "Update my trip plan", "source": "chips"})
        _run(agent, {**base, "question": "Plan my trip", "selected_option_ids": ["x"]})

    def test_no_options_message_says_what_was_searched(self) -> None:
        agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)
        result, _ = _run(agent, {"question": "from Boston to Austin tomorrow"})
        assert result.output["shortlist"] == []
        message = result.output["message"]
        assert "No options found" in message
        assert "Boston → Austin" in message
        assert "2026-11-12" in message


# ---------------------------------------------------------------------------
# UAT-2 M3 — "Actually I'll come back Friday instead" sets the return
# ---------------------------------------------------------------------------

_UAT2_PRIOR = {
    "origin": "ORD",
    "destination": "JFK",
    "departure_date": "2026-11-17",  # Tuesday
    "return_date": "2026-11-19",
    "travelers": 1,
    "currency": "USD",
    "purpose": "Client meeting",
}


class TestReturnWeekdayReanchors:
    def test_come_back_friday_sets_friday_after_departure(self) -> None:
        slots = resolve_slots(
            {},
            text="Actually I'll come back Friday instead",
            prior=_UAT2_PRIOR,
            today=TODAY,
        ).slots
        # Friday after the Tue 2026-11-17 departure — not cleared, not 2026-11-13.
        assert slots["return_date"] == "2026-11-20"
        assert slots["departure_date"] == "2026-11-17"
        assert slots["purpose"] == "Client meeting"

    def test_return_weekday_already_after_departure_unchanged(self) -> None:
        prior = {**_UAT2_PRIOR, "departure_date": "2026-11-12", "return_date": None}
        slots = resolve_slots({}, text="come back on Friday", prior=prior, today=TODAY).slots
        assert slots["return_date"] == "2026-11-13"

    def test_same_message_departure_and_return_weekdays(self) -> None:
        slots = resolve_slots(
            {"origin": "ORD", "destination": "JFK", "travelers": 1, "currency": "USD"},
            text="leave next Tuesday and come back Friday",
            prior=None,
            today=TODAY,
        ).slots
        assert slots["departure_date"] == "2026-11-17"
        assert slots["return_date"] == "2026-11-20"

    def test_internal_key_never_leaks_into_slots(self) -> None:
        resolved = resolve_slots(
            {}, text="Actually I'll come back Friday instead", prior=_UAT2_PRIOR, today=TODAY
        )
        assert "_return_weekday" not in resolved.slots
        assert "_return_weekday" not in resolved.extracted

    def test_explicit_next_weekday_return_is_not_reanchored(self) -> None:
        # "next Friday" is an explicit week; a return before departure is still dropped.
        prior = {**_UAT2_PRIOR, "departure_date": "2026-12-01"}
        slots = resolve_slots({}, text="coming back next Friday", prior=prior, today=TODAY).slots
        assert slots["return_date"] is None
