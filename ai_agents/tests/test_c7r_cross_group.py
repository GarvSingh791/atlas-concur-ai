"""C7R cross-group review — hermetic regressions.

  * PolicyQaCritic verdict parsing tolerates markdown / labels / blank lines and
    runs at POLICY_QA_CRITIC_TEMPERATURE (default 0.0) — not the global 0.3.
  * OpenRouter honours a per-call temperature override.
  * Every policy flag can carry the entry's id (``entry_id``).
  * PolicyEvalHandler passes the unit's receipt_issue (approval-time wording),
    stamps entry_id on flags / items and appends caller-derived extra flags
    (e.g. ``possible_duplicate``).
  * /policy/evaluate labels never collide with a claimant-typed vendor, and an
    entry_id the handler set is never overwritten by label matching.

Regression proof: see each test's docstring for the old behaviour it pins.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import verdicts_to_flags
from ai_agents.agents.policy_eval_handler import PolicyEvalHandler
from ai_agents.agents.policy_qa import PolicyQaCritic, parse_critic_verdict
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import GroundedResult
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from backend.app.modules.policy.service import _unique_entry_labels, attach_entry_ids
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

_ORG = uuid.uuid4()


# ---------------------------------------------------------------------------
# Critic verdict parsing + temperature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("SUPPORTED\nAll claims are in the clause.", "SUPPORTED"),
        ("**SUPPORTED**\nAll claims are in the clause.", "SUPPORTED"),
        ("Verdict: SUPPORTED\nok", "SUPPORTED"),
        ("**Verdict:** SUPPORTED", "SUPPORTED"),
        ('\n\n"SUPPORTED."\nok', "SUPPORTED"),
        ("supported", "SUPPORTED"),
        ("UNSUPPORTED\nnot in the clauses", "UNSUPPORTED"),
        ("**CONTRADICTED** — the clause says 10 hours", "CONTRADICTED"),
        ("NOT SUPPORTED", None),
        ("I think it is SUPPORTED", None),
        ("", None),
    ],
)
def test_critic_verdict_parsing(reply: str, verdict: str | None) -> None:
    """Old parser: lines[0].upper().startswith('SUPPORTED') — '**SUPPORTED**',
    'Verdict: SUPPORTED' and a leading blank line were all read as inconclusive."""
    assert parse_critic_verdict(reply)[0] == verdict


def test_critic_rationale_is_the_rest_of_the_line_or_the_next_line() -> None:
    assert parse_critic_verdict("SUPPORTED — matches AIR-01")[1] == "matches AIR-01"
    assert parse_critic_verdict("**SUPPORTED**\n\nmatches AIR-01")[1] == "matches AIR-01"


class _RecordingLlm(LlmProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.temperatures: list[float | None] = []
        self.plain_calls = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.plain_calls += 1
        return LlmResponse(text=self.reply, model="stub", usage=LlmUsage(1, 1))

    def complete_with_options(
        self, messages: list[LlmMessage], *, temperature: float | None = None
    ) -> LlmResponse:
        self.temperatures.append(temperature)
        return LlmResponse(text=self.reply, model="stub", usage=LlmUsage(1, 1))

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _hypothesis() -> GroundedResult:
    return GroundedResult(
        claim="q",
        value="Economy is standard [AIR-01].",
        citations=(),
        confidence=0.8,
        asserted=False,
    )


def test_critic_runs_at_the_critic_temperature_and_accepts_markdown() -> None:
    """Old critic: provider.complete() at the global LLM_TEMPERATURE (0.3), and a
    '**SUPPORTED**' reply was withheld as inconclusive."""
    llm = _RecordingLlm("**SUPPORTED**\nAll claims are in AIR-01.")
    critique = PolicyQaCritic(provider=llm, clauses=())(_hypothesis())
    assert critique.verdict == "supported"
    assert llm.temperatures == [0.0]
    assert llm.plain_calls == 0


def test_duck_typed_provider_still_works() -> None:
    """A test double that is not an LlmProvider has no options hook: plain complete()."""
    fake = MagicMock(spec=["complete"])
    fake.complete.return_value = LlmResponse(text="SUPPORTED\nok", model="x", usage=LlmUsage(1, 1))
    assert PolicyQaCritic(provider=fake, clauses=())(_hypothesis()).verdict == "supported"


def test_base_provider_default_ignores_options() -> None:
    class _Plain(_RecordingLlm):
        complete_with_options = LlmProvider.complete_with_options  # the default

    llm = _Plain("SUPPORTED")
    llm.complete_with_options([LlmMessage(role="user", content="x")], temperature=0.0)
    assert llm.plain_calls == 1


def test_openrouter_payload_honours_a_per_call_temperature() -> None:
    from services.llm_provider.openrouter import OpenRouterLlmProvider

    settings = MagicMock(LLM_MAX_TOKENS=10, LLM_TEMPERATURE=0.3, LLM_THINKING_BUDGET=0)
    provider = OpenRouterLlmProvider.__new__(OpenRouterLlmProvider)
    msgs = [LlmMessage(role="user", content="x")]
    common: dict[str, Any] = {
        "settings": settings,
        "model": "m",
        "messages": msgs,
        "tools": None,
        "tool_choice": None,
        "stream": False,
    }
    assert provider._build_payload(**common)["temperature"] == 0.3
    assert provider._build_payload(**common, temperature=0.0)["temperature"] == 0.0


# ---------------------------------------------------------------------------
# entry_id on flags + handler wiring
# ---------------------------------------------------------------------------


def _receipt_required_verdict() -> RuleVerdict:
    return RuleVerdict(
        rule_id="r1",
        rule_type="receipt_required",
        applies_to="all",
        source_clause_ref="RECEIPT-REQ",
        in_policy=True,
        reason="receipt required",
        requires_approver=None,
        observed={"receipt_required": True},
    )


def test_verdicts_to_flags_stamps_entry_id_on_every_flag() -> None:
    flags, _ = verdicts_to_flags(
        [_receipt_required_verdict()],
        entry_ref="Corner Deli",
        has_receipt=False,
        category="meals",
        entry_id="e-1",
    )
    assert [fl.to_dict()["entry_id"] for fl in flags] == ["e-1"]


_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.deterministic,
    task_type="policy_evaluate",
    confidence=0.95,
    rationale="t",
)


def _handle(units: list[dict[str, Any]]) -> dict[str, Any]:
    def _evaluate(_self: Any, item: EvalItem, **_k: Any) -> PolicyVerdict:
        return PolicyVerdict(
            item=item,
            in_policy=True,
            verdicts=(_receipt_required_verdict(),),
            evaluated_rule_count=1,
        )

    with (
        patch("ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate", new=_evaluate),
        patch("ai_agents.agents.policy_eval_handler.write_decision_log", return_value=MagicMock()),
    ):
        result = PolicyEvalHandler().handle(
            Task(context_type="policy_qa", payload={"items": units}, signals={}),
            _DECISION,
            context=AgentContext(
                organization_id=_ORG, user_id=uuid.uuid4(), granted_permission_keys=frozenset()
            ),
            session=MagicMock(),
        )
    return dict(result.output)


def _unit(ref: str, entry_id: str | None, **extra: Any) -> dict[str, Any]:
    item = EvalItem(item_type="meals", amount_usd=320.0, ref_id=entry_id)
    return {
        "item": dataclasses.asdict(item),
        "entry_ref": ref,
        "entry_id": entry_id,
        "category": "meals",
        **extra,
    }


def test_handler_uses_the_units_receipt_issue_and_entry_id() -> None:
    """Old handler: verdicts_to_flags without receipt_issue -> 'no receipt attached'
    for an attached receipt that does not cover the claim; no entry_id on flags."""
    issue = "the attached receipt (30.00) does not cover the claimed amount (320.0)"
    out = _handle([_unit("Corner Deli", "e-1", has_receipt=False, receipt_issue=issue)])
    (flag,) = [fl for fl in out["flags"] if fl["flag_type"] == "missing_receipt"]
    assert issue in flag["reason"]
    assert "no receipt attached" not in flag["reason"]
    assert flag["entry_id"] == "e-1"
    assert out["items"][0]["entry_id"] == "e-1"


def test_handler_appends_well_formed_extra_flags_only() -> None:
    dup = {
        "entry_ref": "Corner Deli",
        "entry_id": "e-1",
        "flag_type": "possible_duplicate",
        "severity": "blocking",
        "reason": "Possible duplicate claim",
    }
    out = _handle(
        [_unit("Corner Deli", "e-1", has_receipt=True, extra_flags=[dup, {"junk": 1}, "x"])]
    )
    assert [fl["flag_type"] for fl in out["flags"]] == ["possible_duplicate"]
    assert out["verdict"] == "violation"


# ---------------------------------------------------------------------------
# /policy/evaluate labels
# ---------------------------------------------------------------------------


def _draft(vendor: str | None, amount: float) -> DraftEntry:
    return DraftEntry(
        category="meals",
        amount=amount,
        currency="USD",
        expense_date="2026-06-10",
        vendor=vendor,
        description=None,
        status="draft",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def test_generated_ordinal_never_collides_with_a_typed_vendor() -> None:
    """Old algorithm: [Cafe 10, Cafe 10, vendor 'Cafe (Meals 10.00 USD on 2026-06-10) #2']
    -> two entries both labelled '... #2'."""
    typed = "Cafe (Meals 10.00 USD on 2026-06-10) #2"
    labels = _unique_entry_labels([_draft("Cafe", 10), _draft("Cafe", 10), _draft(typed, 5)])
    assert len(set(labels)) == 3, labels
    assert labels[2] == typed  # the claimant's own (unique) label is kept


def test_attach_entry_ids_never_overwrites_a_handler_entry_id() -> None:
    """Even if two units shared a label, the handler-stamped id wins."""
    units = [{"entry_ref": "dup", "entry_id": "a"}, {"entry_ref": "dup", "entry_id": "b"}]
    output = {
        "items": [{"entry_ref": "dup", "entry_id": "a"}, {"entry_ref": "dup", "entry_id": "b"}],
        "flags": [{"entry_ref": "dup", "entry_id": "a"}, {"entry_ref": "dup", "entry_id": "b"}],
    }
    attach_entry_ids(output, units)
    assert [fl["entry_id"] for fl in output["flags"]] == ["a", "b"]


# ---------------------------------------------------------------------------
# Duplicate-claim lookup fails CLOSED
# ---------------------------------------------------------------------------


def test_duplicate_lookup_error_fails_closed_with_a_blocking_flag() -> None:
    """C7R review: a lookup error used to be logged and read as "no duplicate", so a
    re-claimed purchase could auto-approve on a transient DB error even though the
    top-up docstring promises it is always escalated.  Old: returned None."""
    from contextlib import nullcontext

    from ai_agents.agents.expense_agent.presubmit import (
        DUPLICATE_CHECK_FAILED,
        duplicate_claim_flag,
        duplicate_claim_issue,
    )

    session = MagicMock()
    session.begin_nested.return_value = nullcontext()
    session.scalar.side_effect = RuntimeError("statement timeout")
    session.execute.side_effect = RuntimeError("statement timeout")
    entry = DraftEntry(
        category="meals",
        amount=18.73,
        currency="USD",
        expense_date="2026-06-11",
        vendor="Corner Deli",
        description=None,
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
        entry_id=str(uuid.uuid4()),
    )
    ctx = AgentContext(organization_id=_ORG, granted_permission_keys=frozenset())
    issue = duplicate_claim_issue(entry, context=ctx, session=session, owner_id=uuid.uuid4())
    assert issue == DUPLICATE_CHECK_FAILED
    flag = duplicate_claim_flag(entry, entry_ref="Corner Deli", issue=issue)
    assert flag.severity == "blocking"
