"""Hermetic tests for the Travel Agent (P4-T9) — all 10 verification gates.

No database, no LLM API, no network.
PolicyEvalTool.evaluate_many is patched to avoid DB reads.
InventoryTool uses real seeded mock data (hermetic — no DB).
write_decision_log is patched to avoid DB writes.

Gates:
  1  — Happy path: SFO→JFK shortlist with in/out-of-policy labels + rationales
  2  — Clarify: missing slot returns exactly ONE question, zero options
  3  — No-fabrication: unseeded route returns empty shortlist, no invented options
  4  — Policy honesty: out-of-policy option labeled correctly, never top-ranked
  5  — Reflection catch: mislabeled option corrected by the critic
  6  — (DB-only — see test_travel_agent_db.py)
  7  — Role-bound: traveler_id ≠ user_id → denied
  8  — Decision log: exactly one row per invocation
  9  — Route mix: deterministic path, LLM never called
 10  — Degradation: LLM ProviderError → fallback + needs_review
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.agents.travel_agent import TravelAgent
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.agents.travel_agent.reflection import (
    TravelPolicyHonestyCritic,
    TravelShortlistHypothesizer,
)
from ai_agents.context import AgentContext
from ai_agents.guardrails.reflection import ReflectionOutcome, reflect
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from config.constants.permissions import TRIP_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()

# Seeded flight option_ids for SFO→JFK 2026-06-10
_UA201 = "UA201-2026-06-10"
_DL408 = "DL408-2026-06-10"
_UA201B = "UA201B-2026-06-10"  # out-of-policy (business <6h)

# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Raises AssertionError if ever called — proves LLM is not touched."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on this path")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called on this path")


class _ProviderErrorProvider(LlmProvider):
    """Always raises ProviderError — simulates LLM unavailability (NFR-R1)."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")


class _ScriptedProvider(LlmProvider):
    """Returns pre-scripted responses in sequence (cyclic)."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(
            text=text, model="test-stub", usage=LlmUsage(input_tokens=10, output_tokens=5)
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield from []


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _ctx(*, extra_keys: frozenset[str] | None = None) -> AgentContext:
    keys = frozenset({TRIP_CREATE_OWN}) | (extra_keys or frozenset())
    return AgentContext(
        organization_id=_ORG_ID,
        user_id=_USER_ID,
        granted_permission_keys=keys,
        correlation_id="test",
    )


def _ctx_no_trip_create() -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        user_id=_USER_ID,
        granted_permission_keys=frozenset(),
        correlation_id="test",
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.llm,
    task_type="trip_planning",
    confidence=0.95,
    rationale="context_type=travel_planning",
)


def _task(payload: dict) -> Task:
    return Task(context_type="travel_planning", payload=payload)


def _sfo_jfk_payload(**overrides: Any) -> dict:
    base = {
        "origin": "SFO",
        "destination": "JFK",
        "departure_date": "2026-06-10",
        "travelers": 1,
        "currency": "USD",
        "include_hotels": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Verdict factories
# ---------------------------------------------------------------------------


def _in_policy_verdict(ref_id: str, amount: float, duration: float = 5.5) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(
            item_type="flight",
            amount_usd=amount,
            cabin_class="economy",
            duration_hours=duration,
            ref_id=ref_id,
        ),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="cabin_class",
                applies_to="flight",
                source_clause_ref="AIR-CABIN",
                in_policy=True,
                reason="Economy class permitted for 5.5h flight.",
                requires_approver=None,
                observed={"cabin_class": "economy", "duration_hours": duration},
            ),
        ),
        evaluated_rule_count=1,
    )


def _out_policy_verdict(ref_id: str, amount: float, duration: float = 5.5) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(
            item_type="flight",
            amount_usd=amount,
            cabin_class="business",
            duration_hours=duration,
            ref_id=ref_id,
        ),
        in_policy=False,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="cabin_class",
                applies_to="flight",
                source_clause_ref="AIR-CABIN",
                in_policy=False,
                reason="Business class not permitted for flights under 6h.",
                requires_approver="VP",
                observed={"cabin_class": "business", "duration_hours": duration},
            ),
        ),
        evaluated_rule_count=1,
    )


# ---------------------------------------------------------------------------
# Gate 1 — Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_sfo_jfk_shortlist(self) -> None:
        """SFO→JFK request returns a shortlist with in/out-of-policy labels."""
        verdicts = (
            _in_policy_verdict(_UA201, 320.0),
            _in_policy_verdict(_DL408, 298.0),
            _out_policy_verdict(_UA201B, 1450.0),
        )
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.status == AgentResultStatus.ok
        assert not result.output.get("clarify")
        shortlist = result.output["shortlist"]
        assert len(shortlist) == 3, f"expected 3 options, got {len(shortlist)}"

        # All recommended option_ids are from the seeded inventory
        returned_ids = {o["option_id"] for o in shortlist}
        assert returned_ids == {_UA201, _DL408, _UA201B}, "fabricated option detected"

        # Out-of-policy option is labeled correctly
        ua201b = next(o for o in shortlist if o["option_id"] == _UA201B)
        assert ua201b["in_policy"] is False
        assert ua201b["policy_label"] == "Out of Policy"
        assert "AIR-CABIN" in ua201b["clause_refs"]

        # In-policy options are labeled correctly
        for opt in (o for o in shortlist if o["option_id"] in {_UA201, _DL408}):
            assert opt["in_policy"] is True
            assert opt["policy_label"] == "In Policy"

        # Citations are present
        assert len(result.citations) > 0

    def test_in_policy_options_ranked_before_out_of_policy(self) -> None:
        """In-policy options must rank above out-of-policy ones (FR-T4)."""
        verdicts = (
            _in_policy_verdict(_UA201, 320.0),
            _in_policy_verdict(_DL408, 298.0),
            _out_policy_verdict(_UA201B, 1450.0),
        )
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        shortlist = result.output["shortlist"]
        in_policy_ranks = [o["rank"] for o in shortlist if o["in_policy"]]
        out_policy_ranks = [o["rank"] for o in shortlist if not o["in_policy"]]
        assert max(in_policy_ranks) < min(
            out_policy_ranks
        ), "Out-of-policy option ranked higher than an in-policy option"


# ---------------------------------------------------------------------------
# Gate 2 — Clarify
# ---------------------------------------------------------------------------


class TestClarify:
    def test_missing_destination_returns_one_question(self) -> None:
        """A request missing a required slot returns exactly ONE question and no options."""
        payload = {
            "origin": "SFO",
            # destination intentionally absent
            "departure_date": "2026-06-10",
            "travelers": 1,
            "currency": "USD",
        }
        with patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.status == AgentResultStatus.ok
        assert result.output.get("clarify") is True
        question = result.output.get("question", "")
        assert isinstance(question, str) and len(question) > 0, "expected a clarifying question"
        assert "shortlist" not in result.output or result.output.get("shortlist") is None

    def test_missing_departure_date_returns_question(self) -> None:
        """Missing departure_date → question asking for the date (not destination)."""
        payload = {
            "origin": "SFO",
            "destination": "JFK",
            # departure_date absent
            "travelers": 1,
            "currency": "USD",
        }
        with patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.output.get("clarify") is True
        # Should ask about departure date (second missing slot)
        question = result.output.get("question", "")
        assert "date" in question.lower(), f"expected date question, got: {question!r}"

    def test_complete_payload_does_not_clarify(self) -> None:
        """A fully specified payload does NOT trigger the clarify path."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert not result.output.get("clarify"), "should not clarify when all slots are present"


# ---------------------------------------------------------------------------
# Gate 3 — No fabrication
# ---------------------------------------------------------------------------


class TestNoFabrication:
    def test_unseeded_route_returns_empty_shortlist(self) -> None:
        """An unseeded origin/destination/date returns 'no options found', no invented fare."""
        payload = {
            "origin": "BOS",
            "destination": "LAX",
            "departure_date": "2030-01-01",  # no seed data for this date
            "travelers": 1,
            "currency": "USD",
            "include_hotels": False,
        }
        with patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.status == AgentResultStatus.ok
        shortlist = result.output.get("shortlist", [])
        assert shortlist == [], f"expected empty shortlist, got {shortlist}"
        assert "message" in result.output

    def test_recommended_ids_are_in_seeded_inventory(self) -> None:
        """Every recommended option_id must come from the inventory tool, not be invented."""
        # Seeded inventory option_ids for SFO→JFK 2026-06-10
        seeded_ids = {_UA201, _DL408, _UA201B}
        verdicts = (
            _in_policy_verdict(_UA201, 320.0),
            _in_policy_verdict(_DL408, 298.0),
            _out_policy_verdict(_UA201B, 1450.0),
        )
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        shortlist = result.output["shortlist"]
        returned_ids = {o["option_id"] for o in shortlist}
        assert returned_ids.issubset(
            seeded_ids
        ), f"Fabricated option IDs detected: {returned_ids - seeded_ids}"


# ---------------------------------------------------------------------------
# Gate 4 — Policy honesty
# ---------------------------------------------------------------------------


class TestPolicyHonesty:
    def test_cheapest_out_of_policy_is_labeled_and_not_top_ranked(self) -> None:
        """When the cheapest option is out-of-policy, it is labeled and not top-ranked."""
        # Option A is cheapest but out-of-policy; B is more expensive but in-policy.
        # UA201B: most expensive; DL408 ($298): cheapest in-policy; UA201 ($320): in-policy.
        # Out-of-policy option should always rank below in-policy options.
        verdicts = (
            _in_policy_verdict(_UA201, 320.0),
            _in_policy_verdict(_DL408, 298.0),
            _out_policy_verdict(_UA201B, 200.0),  # cheapest price, but out-of-policy
        )
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        shortlist = result.output["shortlist"]
        ua201b = next(o for o in shortlist if o["option_id"] == _UA201B)

        # Labeled out-of-policy
        assert ua201b["in_policy"] is False, "cheapest option should be labeled out-of-policy"
        assert ua201b["policy_label"] == "Out of Policy"
        assert ua201b["policy_reason"], "policy reason must be non-empty"

        # Not top-ranked (in-policy options rank above it)
        assert (
            ua201b["rank"] > 1
        ), f"out-of-policy cheapest option ranked {ua201b['rank']}; should not be #1"

        # In-policy options rank above it
        in_policy = [o for o in shortlist if o["in_policy"]]
        assert all(
            o["rank"] < ua201b["rank"] for o in in_policy
        ), "Some in-policy option ranked below the out-of-policy cheapest option"


# ---------------------------------------------------------------------------
# Gate 5 — Reflection catch
# ---------------------------------------------------------------------------


class TestReflectionCatch:
    def _make_option(
        self,
        option_id: str,
        in_policy: bool,
        rule_in_policy: bool,
        amount_usd: float = 300.0,
    ) -> RankedOption:
        return RankedOption(
            option_id=option_id,
            segment_type="flight",
            supplier="Test Airline",
            amount_usd=amount_usd,
            sustainability_score=3,
            cabin_class="economy",
            duration_hours=5.5,
            is_direct=True,
            city=None,
            nightly_rate_usd=None,
            room_type=None,
            in_policy=in_policy,
            policy_reason="Test reason" if not rule_in_policy else "",
            clause_refs=("AIR-CABIN",),
            requires_approver="VP" if not rule_in_policy else None,
            rule_in_policy=rule_in_policy,
            rank_score=100.0 if in_policy else 0.0,
            explanation="Test explanation.",
        )

    def test_mislabeled_in_policy_option_caught_and_corrected(self) -> None:
        """Critic catches an option labeled in_policy=True but rule_in_policy=False."""
        # Option A: mislabeled — display says in_policy but verdict says out
        opt_a = self._make_option(
            "MISLABELED", in_policy=True, rule_in_policy=False, amount_usd=200.0
        )
        opt_b = self._make_option("CORRECT", in_policy=True, rule_in_policy=True, amount_usd=300.0)
        shortlist = [opt_a, opt_b]

        hyp = TravelShortlistHypothesizer(shortlist)
        critic = TravelPolicyHonestyCritic()
        result = reflect(hypothesizer=hyp, critic=critic)

        # Outcome must not be "passed" — must be revised (one round of correction)
        assert result.outcome in {
            ReflectionOutcome.revised,
            ReflectionOutcome.rejected,
        }, f"Expected revised/rejected but got {result.outcome}"

        corrected: list[RankedOption] = result.value  # type: ignore[assignment]
        assert isinstance(corrected, list)

        mislabeled_corrected = next(o for o in corrected if o.option_id == "MISLABELED")
        # The corrected label must now match the rule verdict
        assert (
            mislabeled_corrected.in_policy is False
        ), "Reflection critic should have corrected in_policy=False"
        assert mislabeled_corrected.rule_in_policy is False

    def test_clean_shortlist_passes_reflection(self) -> None:
        """A correctly labeled shortlist passes the critic (outcome=passed)."""
        opt_a = self._make_option("CORRECT-IP", in_policy=True, rule_in_policy=True)
        opt_b = self._make_option(
            "CORRECT-OOP", in_policy=False, rule_in_policy=False, amount_usd=100.0
        )
        shortlist = [opt_a, opt_b]

        result = reflect(
            hypothesizer=TravelShortlistHypothesizer(shortlist),
            critic=TravelPolicyHonestyCritic(),
        )
        assert result.outcome == ReflectionOutcome.passed
        assert result.asserted is True


# ---------------------------------------------------------------------------
# Gate 7 — Role-bound
# ---------------------------------------------------------------------------


class TestRoleBound:
    def test_different_traveler_id_is_blocked(self) -> None:
        """traveler_id ≠ context.user_id → status=needs_review, denied=True."""
        other_user_id = str(uuid.uuid4())
        payload = {**_sfo_jfk_payload(), "traveler_id": other_user_id}
        with patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True

    def test_same_traveler_id_is_allowed(self) -> None:
        """traveler_id == str(context.user_id) → proceeds normally."""
        payload = {**_sfo_jfk_payload(), "traveler_id": str(_USER_ID)}
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(payload),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.output.get("denied") is not True


# ---------------------------------------------------------------------------
# Gate 8 — Decision log
# ---------------------------------------------------------------------------


class TestDecisionLog:
    def test_one_log_row_per_happy_path_invocation(self) -> None:
        """Exactly one ai_decision_logs row is written per handle() call."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert mock_log.call_count == 1, f"Expected 1 decision log call, got {mock_log.call_count}"
        record = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.travel
        assert record.task_type == "trip_planning"
        assert record.route is not None
        assert "shortlist_count" in (record.output_summary or {})

    def test_one_log_row_on_clarify_path(self) -> None:
        """Clarify path also writes exactly one decision log row."""
        payload = {"origin": "SFO", "travelers": 1, "currency": "USD"}
        with patch(
            "ai_agents.agents.travel_agent.agent.write_decision_log",
            return_value=MagicMock(),
        ) as mock_log:
            agent = TravelAgent(provider=_RaisingProvider())
            agent.handle(_task(payload), _DECISION, context=_ctx(), session=MagicMock())

        assert mock_log.call_count == 1

    def test_decision_log_has_tools_used(self) -> None:
        """Decision log record includes tools_used with InventoryTool and PolicyEvalTool."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            agent.handle(
                _task(_sfo_jfk_payload()),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        record = mock_log.call_args.kwargs["record"]
        tools = record.tools_used or {}
        assert any(
            "InventoryTool" in k for k in tools
        ), f"InventoryTool not found in tools_used: {tools}"
        assert "PolicyEvalTool" in tools


# ---------------------------------------------------------------------------
# Gate 9 — Route mix
# ---------------------------------------------------------------------------


class TestRouteMix:
    def test_deterministic_path_does_not_call_llm(self) -> None:
        """Without explain_with_llm, the agent stays on deterministic route."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            # _RaisingProvider raises if called — proves LLM is never touched
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload(explain_with_llm=False)),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert result.status in {AgentResultStatus.ok, AgentResultStatus.needs_review}
        record = mock_log.call_args.kwargs["record"]
        assert (
            record.route == AiRoute.deterministic
        ), f"Expected deterministic route, got {record.route}"

    def test_llm_path_recorded_as_llm_route(self) -> None:
        """With explain_with_llm=True and a working provider, route=llm is recorded."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        scripted = _ScriptedProvider("This flight offers good value and is in-policy.")
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ) as mock_log,
        ):
            agent = TravelAgent(provider=scripted)
            agent.handle(
                _task(_sfo_jfk_payload(explain_with_llm=True)),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        record = mock_log.call_args.kwargs["record"]
        assert record.route == AiRoute.llm, f"Expected llm route, got {record.route}"
        assert record.model_provider is not None


# ---------------------------------------------------------------------------
# Gate 10 — Degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_llm_provider_error_falls_back_gracefully(self) -> None:
        """LLM ProviderError → templated fallback + status=needs_review; no crash (NFR-R1)."""
        verdicts = (
            _in_policy_verdict(_UA201, 320.0),
            _in_policy_verdict(_DL408, 298.0),
        )
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_ProviderErrorProvider())
            result = agent.handle(
                _task(_sfo_jfk_payload(explain_with_llm=True)),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert (
            result.status == AgentResultStatus.needs_review
        ), "Degraded agent should return needs_review"
        # Shortlist still populated (graceful fallback, not a crash)
        shortlist = result.output.get("shortlist", [])
        assert len(shortlist) == 2, f"shortlist should still have 2 options, got {len(shortlist)}"
        assert result.output.get("degraded") is True

    def test_full_flow_does_not_crash_on_provider_error(self) -> None:
        """End-to-end: ProviderError during narrative does not raise; returns a result."""
        verdicts = (_in_policy_verdict(_UA201, 320.0),)
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=verdicts,
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_ProviderErrorProvider())
            # Should not raise
            result = agent.handle(
                _task(_sfo_jfk_payload(explain_with_llm=True)),
                _DECISION,
                context=_ctx(),
                session=MagicMock(),
            )

        assert isinstance(result.output, dict)
        assert "shortlist" in result.output


# ---------------------------------------------------------------------------
# Layering check (Gate 12 equivalent)
# ---------------------------------------------------------------------------


def test_travel_agent_importable_without_backend() -> None:
    """ai_agents.agents.travel_agent is importable without importing backend."""
    import subprocess  # noqa: E401
    import sys

    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ai_agents.agents.travel_agent import TravelAgent; "
            "import sys; "
            "assert not any('backend' in m for m in sys.modules), "
            "'backend imported by travel_agent'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, f"Import check failed:\n{check.stdout}\n{check.stderr}"
