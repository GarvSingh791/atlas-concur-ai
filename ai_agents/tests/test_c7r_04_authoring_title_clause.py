"""C7R-04 — AI rule authoring: human-readable title + corpus-validated clause ref (hermetic).

Staging re-acceptance: the drafted rule's title was just its rule_type
("amount_cap"), and the LLM copied an arbitrary user prefix ("C7R") into
source_clause_ref — it passed the purely syntactic C7-04 sanitizer.

Contract pinned here:
  * ``draft_rule_title`` derives a readable title from the structured rule
    (e.g. "Hotel cap — Tier 2 cities $275/night"), never the bare rule_type;
  * ``resolve_clause_ref`` keeps a ref ONLY when it is a real corpus clause id
    (canonical casing), otherwise None — "C7R" and other user text never survive;
  * ``persist_draft_rule`` validates against the supplied corpus ids, and when none
    are supplied it loads them from the DB and FAILS CLOSED (no corpus -> None);
  * the critic cites a clause only when it validated against the corpus ids;
  * the authoring prompt forbids copying labels from the intent.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from ai_agents.agents.policy_authoring import (
    _AUTHOR_PROMPT,
    RuleAuthoringCritic,
    draft_rule_title,
    load_corpus_clause_refs,
    persist_draft_rule,
    render_author_prompt,
    resolve_clause_ref,
)
from ai_agents.guardrails.citations import CitationKind, GroundedResult
from services.llm_provider.interface import LlmMessage, LlmProvider, LlmResponse, LlmUsage

_CORPUS = frozenset({"HOTEL-CAP", "MEALS-PD", "LODG-03", "GRND-02"})

_HOTEL_T2 = {
    "rule_type": "amount_cap",
    "applies_to": "hotel",
    "definition": {
        "currency": "USD",
        "per": "night",
        "tiers": [{"tier": "tier_2", "cap": 275}],
        "exception_approver": "manager",
    },
    "source_clause_ref": "C7R",
}


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_type", "applies_to", "definition", "expected"),
    [
        ("amount_cap", "hotel", _HOTEL_T2["definition"], "Hotel cap — Tier 2 cities $275/night"),
        (
            "amount_cap",
            "car",
            {"currency": "USD", "per": "day", "tiers": [{"tier": "standard", "cap": 150}]},
            "Car rental cap — Standard $150/day",
        ),
        (
            "amount_cap",
            "hotel",
            {
                "currency": "USD",
                "per": "night",
                "tiers": [
                    {"tier": "tier1", "cap": 250},
                    {"tier": "domestic", "cap": 180},
                    {"tier": "intl_high", "cap": 300},
                ],
            },
            "Hotel cap — Tier 1 cities $250, Domestic $180, Intl high $300/night",
        ),
        (
            "amount_cap",
            "meals",
            {"currency": "EUR", "per": "day", "tiers": [{"tier": "standard", "cap": 60}]},
            "Meals cap — Standard EUR 60/day",
        ),
        (
            "per_diem",
            "meals",
            {
                "currency": "USD",
                "per": "day",
                "rates": [
                    {"region": "domestic", "amount": 75},
                    {"region": "international", "amount": 100},
                ],
            },
            "Meals per diem — Domestic $75, International $100/day",
        ),
        (
            "cabin_class",
            "flight",
            {
                "tiers": [
                    {"max_hours": 6, "allowed_classes": ["economy"]},
                    {"min_hours": 6, "allowed_classes": ["economy", "premium_economy"]},
                ]
            },
            "Flight cabin class — Economy under 6h; up to Premium economy from 6h",
        ),
        (
            "preferred_supplier",
            "hotel",
            {"preferred_suppliers": ["Marriott International", "Hilton"]},
            "Preferred hotel suppliers — Marriott International, Hilton",
        ),
        (
            "category_allow",
            "car",
            {"allowed_classes": ["Economy", "Standard"], "excluded_classes": ["SUV"]},
            "Car rental classes — Economy, Standard allowed",
        ),
        (
            "receipt_required",
            "all",
            {"threshold_usd": 25, "always_required_categories": ["airfare"]},
            "Receipts required — $25 and over",
        ),
    ],
)
def test_title_is_human_readable(
    rule_type: str, applies_to: str, definition: dict, expected: str
) -> None:
    assert draft_rule_title(rule_type, applies_to, definition) == expected


@pytest.mark.parametrize(
    ("rule_type", "applies_to", "definition"),
    [
        ("amount_cap", "hotel", {}),
        ("amount_cap", "hotel", {"tiers": "junk"}),
        ("cabin_class", "flight", None),
        ("per_diem", "meals", {"rates": [{"region": None}]}),
        ("preferred_supplier", "flight", {"preferred_suppliers": []}),
        ("receipt_required", "all", {"threshold_usd": "x"}),
    ],
)
def test_title_degrades_to_a_readable_generic_title(
    rule_type: str, applies_to: str, definition: Any
) -> None:
    title = draft_rule_title(rule_type, applies_to, definition)
    assert title and title != rule_type
    assert "_" not in title  # never a raw enum value


def test_title_accepts_enum_members_and_is_bounded() -> None:
    from database.models.policy_rule import PolicyAppliesTo, PolicyRuleType

    title = draft_rule_title(
        PolicyRuleType.preferred_supplier,
        PolicyAppliesTo.flight,
        {"preferred_suppliers": [f"Carrier number {i}" for i in range(40)]},
    )
    assert title.startswith("Preferred airline suppliers — Carrier number 0")
    assert len(title) <= 120


# ---------------------------------------------------------------------------
# Clause ref validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("HOTEL-CAP", "HOTEL-CAP"),
        (" hotel-cap ", "HOTEL-CAP"),  # canonical casing from the corpus
        ("C7R", None),  # the staging defect: an arbitrary user prefix
        ("POL-001", None),  # the prompt's own example label is not a corpus id
        ("X'); DROP TABLE x;--", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_resolve_clause_ref_only_keeps_real_corpus_ids(value: Any, expected: str | None) -> None:
    assert resolve_clause_ref(value, _CORPUS) == expected


def test_resolve_clause_ref_with_empty_corpus_fails_closed() -> None:
    assert resolve_clause_ref("HOTEL-CAP", frozenset()) is None


def test_persist_validates_against_supplied_corpus_ids() -> None:
    session = MagicMock()
    rule = persist_draft_rule(
        candidate=_HOTEL_T2,
        policy_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        session=session,
        known_clause_refs=_CORPUS,
    )
    assert rule.source_clause_ref is None
    ok = persist_draft_rule(
        candidate={**_HOTEL_T2, "source_clause_ref": "hotel-cap"},
        policy_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        session=session,
        known_clause_refs=_CORPUS,
    )
    assert ok.source_clause_ref == "HOTEL-CAP"


def test_persist_without_corpus_ids_loads_them_and_fails_closed() -> None:
    session = MagicMock()
    session.scalars.side_effect = RuntimeError("db down")
    rule = persist_draft_rule(
        candidate={**_HOTEL_T2, "source_clause_ref": "HOTEL-CAP"},
        policy_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        session=session,
    )
    assert rule.source_clause_ref is None


def test_load_corpus_clause_refs_filters_non_strings_and_blank() -> None:
    session = MagicMock()
    session.scalars.return_value.all.return_value = ["HOTEL-CAP", None, "", "  ", "MEALS-PD", 7]
    assert load_corpus_clause_refs(session, policy_id=uuid.uuid4()) == frozenset(
        {"HOTEL-CAP", "MEALS-PD"}
    )


# ---------------------------------------------------------------------------
# Critic + prompt
# ---------------------------------------------------------------------------


class _Faithful(LlmProvider):
    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        return LlmResponse(
            text="FAITHFUL\nok", model="s", usage=LlmUsage(input_tokens=1, output_tokens=1)
        )

    def stream(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        yield from []


def _critique(ref: str, known: frozenset[str] | None) -> list[str]:
    critic = RuleAuthoringCritic(provider=_Faithful(), intent="Tier 2 hotel cap $275")
    if known is not None:
        critic = RuleAuthoringCritic(
            provider=_Faithful(), intent="Tier 2 hotel cap $275", known_clause_refs=known
        )
    out = critic(
        GroundedResult(
            claim="x",
            value={**_HOTEL_T2, "source_clause_ref": ref},
            citations=(),
            confidence=0.8,
            asserted=False,
        )
    )
    assert out.verdict == "supported"
    assert out.correction is not None
    return [c.ref for c in out.correction.citations if c.kind == CitationKind.clause_ref]


def test_critic_cites_only_corpus_validated_clauses() -> None:
    assert _critique("C7R", _CORPUS) == []
    assert _critique("HOTEL-CAP", _CORPUS) == ["HOTEL-CAP"]
    # No corpus supplied -> an unvalidated label is not evidence.
    assert _critique("HOTEL-CAP", None) == []


def test_prompt_forbids_copying_labels_from_the_intent() -> None:
    text = _AUTHOR_PROMPT.lower()
    assert "never copy" in text
    assert "null" in text
    assert '"pol-001"' not in text  # no invented example label to imitate
    # The prompt still renders (Issue 5: placeholder substitution, intent last).
    rendered = render_author_prompt("C7R hotel")
    assert "C7R hotel" in rendered
    json.dumps(rendered)
