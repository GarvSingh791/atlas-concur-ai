"""Grounding false-block fixes on the calibrated (live) path.

The keyed live eval fell from 113/145 grounded (R6) to 67/145 with faithfulness unchanged.  A
per-question audit of the withheld drafts (real Gemini drafts, record/replay) found that most
were correct, cited answers rejected by four mechanical false-negative classes.  Each class is
pinned here against the REAL Helios corpus text, together with the adversarial counterpart the
original guard exists for (which must still be caught):

  * ``TestCitationParsing``  — grouped markers ``[T&E FAQ §FAQ-Q03, T&E Policy §AIR-02]`` and the
    trailing style ``"A. [§X] B. [§Y]"`` (a marker after the full stop); the leading style keeps
    its attribution.
  * ``TestSectionCitations`` — the generator is shown each policy clause's whole section, so it
    may cite a section sibling it read there (MIL-02 when only MIL-01 was retrieved).
  * ``TestContrastRestatement`` — an FAQ / How-To / Reference unit that restates ONLY the cited
    rule ("... (see AIR-03)") is the same rule, not a mis-attribution source.
  * ``TestStatementPolarity`` — FAQ question lines assert nothing; "no more than 3" is a bound;
    "X is reimbursable, but Y is not" is two statements; a positive statement is matched to the
    evidence statement it restates most closely.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config.settings as cs
from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.rag.grounding import extract_cited_sentences, section_citations
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from ai_agents.rag.support import SupportContext, SupportInput, assess_sentence
from database.models.ai_decision_log import AiAgent, AiRoute
from database.seeds.corpus import ALL_DOCUMENTS
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse

_UNITS: dict[str, str] = {u.id: u.text for d in ALL_DOCUMENTS for u in d.units}
_THRESHOLD = 0.55


def _cit(ref: str, detail: str | None = None) -> Citation:
    return Citation(
        kind=CitationKind.clause_ref,
        ref=ref,
        detail=_UNITS.get(ref, f"{ref} text") if detail is None else detail,
        source="Policy",
        document_type="policy",
    )


_DERIVATIVE = {d.document_type for d in ALL_DOCUMENTS} - {"policy"}
_DOC_TYPE: dict[str, str] = {u.id: d.document_type for d in ALL_DOCUMENTS for u in d.units}


def _assess(
    sentence: str,
    refs: tuple[str, ...],
    question: str = "",
    retrieved: tuple[str, ...] = (),
    *,
    typed: bool = True,
) -> Any:
    return assess_sentence(
        0,
        SupportInput(sentence, refs, tuple(_UNITS[r] for r in refs)),
        SupportContext(
            question,
            tuple((r, _UNITS[r]) for r in retrieved),
            derivative_refs=frozenset(
                r for r in retrieved if typed and _DOC_TYPE[r] in _DERIVATIVE
            ),
        ),
        threshold=_THRESHOLD,
    )


def _parse(text: str, refs: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    answer = extract_cited_sentences(text, tuple(_cit(r) for r in refs))
    return [(s.text, [c.ref for c in s.citations]) for s in answer.sentences]


# ------------------------------------------------------------------------------ citation parse


class TestCitationParsing:
    def test_grouped_marker_cites_every_known_ref(self) -> None:
        parsed = _parse(
            "Premium economy is permitted on flights of 6–10 hours with manager approval "
            "[T&E FAQ §FAQ-Q03, T&E Policy §AIR-02].",
            ("FAQ-Q03", "AIR-02"),
        )
        assert parsed == [(parsed[0][0], ["FAQ-Q03", "AIR-02"])]

    def test_grouped_marker_drops_unknown_refs(self) -> None:
        parsed = _parse("Economy for short flights [T&E Policy §AIR-01; §AIR-99].", ("AIR-01",))
        assert parsed[0][1] == ["AIR-01"]

    def test_grouped_marker_is_stripped_before_support_scoring(self) -> None:
        r = _assess(
            "Premium economy is permitted on flights of 6–10 hours with manager approval "
            "[T&E FAQ §FAQ-Q03, T&E Policy §AIR-02].",
            ("FAQ-Q03", "AIR-02"),
        )
        assert r.supported, r
        assert "faq" not in r.unsupported_terms

    def test_trailing_markers_attach_to_the_sentence_before_them(self) -> None:
        parsed = _parse(
            "Yes, every trip requires manager Pre-Trip Approval before booking. "
            "[T&E FAQ §FAQ-Q10] This approval must be obtained before booking. "
            "[T&E Policy §PTA-01]",
            ("FAQ-Q10", "PTA-01"),
        )
        assert [refs for _, refs in parsed] == [["FAQ-Q10"], ["PTA-01"]]
        assert parsed[0][0].endswith("[T&E FAQ §FAQ-Q10]")

    def test_leading_markers_keep_their_sentence(self) -> None:
        # Not trailing style (the answer does not END with a bare marker): a marker that opens
        # a sentence belongs to it — Intro stays uncited, never inherits A's citation.
        parsed = _parse(
            "Intro. [T&E Policy §AIR-01] A is stated. [T&E Policy §AIR-02] B is stated.",
            ("AIR-01", "AIR-02"),
        )
        assert [refs for _, refs in parsed] == [[], ["AIR-01"], ["AIR-02"]]

    def test_inline_then_leading_markers_keep_their_sentence(self) -> None:
        parsed = _parse(
            "A is stated [T&E Policy §AIR-01]. Note this. [T&E Policy §AIR-02] B is stated.",
            ("AIR-01", "AIR-02"),
        )
        assert [refs for _, refs in parsed] == [["AIR-01"], [], ["AIR-02"]]

    def test_trailing_style_never_steals_from_an_already_cited_sentence(self) -> None:
        parsed = _parse(
            "A is stated [T&E Policy §AIR-01]. [T&E Policy §AIR-02] B is stated. "
            "[T&E Policy §AIR-03]",
            ("AIR-01", "AIR-02", "AIR-03"),
        )
        # A already cites AIR-01, so the AIR-02 marker stays with B; B cites AIR-02 itself, so
        # the final bare marker is left on its own (the historical, conservative parse).
        assert [refs for _, refs in parsed] == [["AIR-01"], ["AIR-02"], ["AIR-03"]]

    def test_reattached_citation_is_still_checked_against_the_sentence(self) -> None:
        # A wrong trailing citation does not ground anything: the support gate reads the clause.
        answer = extract_cited_sentences(
            "Business class is permitted on any flight. [T&E Policy §AIR-01]",
            (_cit("AIR-01"),),
        )
        assert answer.sentences[0].citations[0].ref == "AIR-01"
        r = _assess(answer.sentences[0].text, ("AIR-01",), retrieved=("AIR-01", "AIR-03"))
        assert not r.supported


# ------------------------------------------------------------------------- section citations


def _section(*refs: str) -> str:
    return "\n\n".join(_UNITS[r] for r in refs)


class TestSectionCitations:
    def test_section_siblings_become_verifiable_citations(self) -> None:
        clause = GroundedClause(
            clause_ref="MIL-01",
            chunk_text=_section("MIL-01", "MIL-02", "MIL-03"),
            similarity=0.5,
            document_type="policy",
        )
        out = section_citations((clause,), (_cit("MIL-01"),))
        assert [c.ref for c in out] == ["MIL-01", "MIL-02", "MIL-03"]
        assert out[1].detail == _UNITS["MIL-02"]  # exactly the unit text the model was shown
        assert out[1].source == "Policy"

    def test_no_expansion_and_no_duplicates(self) -> None:
        leaf = GroundedClause(
            clause_ref="FAQ-Q12", chunk_text=_UNITS["FAQ-Q12"], similarity=0.5, document_type="faq"
        )
        section = GroundedClause(
            clause_ref="MIL-02",
            chunk_text=_section("MIL-01", "MIL-02"),
            similarity=0.5,
            document_type="policy",
        )
        out = section_citations((leaf, section), (_cit("FAQ-Q12"), _cit("MIL-02")))
        assert [c.ref for c in out] == ["FAQ-Q12", "MIL-02", "MIL-01"]

    def test_agent_grounds_a_draft_citing_a_shown_sibling(self) -> None:
        question = "At what rate is personal-vehicle mileage reimbursed?"
        draft = (
            "Personal-vehicle mileage is reimbursed at the prevailing IRS standard mileage rate "
            "[T&E Policy §MIL-01]. Only business miles count, and normal commute miles are "
            "deducted [T&E Policy §MIL-02]."
        )
        result, records = _run_calibrated(question, "MIL-01", _section("MIL-01", "MIL-02"), draft)
        assert result.output["answer_type"] == "grounded", records[0].output_summary
        assert {c.ref for c in result.citations} == {"MIL-01", "MIL-02"}

    def test_agent_still_rejects_an_unsupported_sibling_claim(self) -> None:
        question = "At what rate is personal-vehicle mileage reimbursed?"
        draft = (
            "Personal-vehicle mileage is reimbursed at the prevailing IRS standard mileage rate "
            "[T&E Policy §MIL-01]. Commute miles are reimbursed at $1.50 per mile "
            "[T&E Policy §MIL-02]."
        )
        result, _records = _run_calibrated(question, "MIL-01", _section("MIL-01", "MIL-02"), draft)
        assert result.output["answer_type"] != "grounded"


# --------------------------------------------------------------------------- contrast guard


class TestContrastRestatement:
    def test_wording_from_a_restatement_of_the_cited_rule_is_not_contrast(self) -> None:
        r = _assess(
            "Yes, business class is permitted for flights longer than 10 hours with VP approval "
            "[T&E Policy §AIR-03].",
            ("AIR-03",),
            question="Am I allowed business class on a flight longer than ten hours?",
            retrieved=("AIR-03", "FAQ-Q01", "AIR-04", "FAQ-Q04", "AIR-02"),
        )
        assert r.supported, r

    def test_policy_restatement_of_a_cited_faq_is_not_contrast(self) -> None:
        r = _assess(
            "One checked bag and standard seat selection are reimbursable [T&E FAQ §FAQ-Q07].",
            ("FAQ-Q07",),
            retrieved=("FAQ-Q07", "AIR-07", "AIR-01"),
        )
        assert r.supported, r

    @pytest.mark.parametrize(
        "sentence",
        [
            "You should book Premium economy for a 3-hour domestic flight [T&E Policy §AIR-01].",
            "Book Business class for a 3-hour flight [T&E Policy §AIR-01].",
        ],
    )
    def test_multi_rule_howto_does_not_launder_a_borrowed_cabin(self, sentence: str) -> None:
        # HOW-07 names AIR-01 but ALSO the premium / business rules: it is not a restatement of
        # AIR-01, so "premium" / "business" stay borrowed from a different rule.
        r = _assess(
            sentence,
            ("AIR-01",),
            question="Which cabin class should I book for a 3-hour domestic flight?",
            retrieved=("AIR-01", "AIR-02", "AIR-03", "HOW-07"),
        )
        assert not r.supported
        assert {"contrast", "unanchored"} & set(r.failed)

    def test_borrowed_approver_from_a_different_rule_is_still_contrast(self) -> None:
        r = _assess(
            "Premium economy is permitted for flights of 6–10 hours with VP approval "
            "[T&E Policy §AIR-02].",
            ("AIR-02",),
            retrieved=("AIR-02", "AIR-03", "FAQ-Q03"),
        )
        assert "contrast" in r.failed

    def test_policy_cross_reference_is_not_a_restatement(self) -> None:
        # CARD-03 names EXP-01 ("... within the EXP-01 deadline") but is a policy clause with
        # its own rule: the card reconciliation borrowed from it while citing EXP-01 is still a
        # mis-attribution.
        r = _assess(
            "Submit expense reports within 30 days of trip completion and reconcile all card "
            "transactions [T&E Policy §EXP-01].",
            ("EXP-01",),
            retrieved=("EXP-01", "CARD-03", "EXP-02"),
        )
        assert "contrast" in r.failed

    def test_unknown_document_types_fail_closed(self) -> None:
        # Without document types no retrieved unit is treated as a restatement.
        r = _assess(
            "Yes, business class is permitted for flights longer than 10 hours with VP approval "
            "[T&E Policy §AIR-03].",
            ("AIR-03",),
            question="Am I allowed business class on a flight longer than ten hours?",
            retrieved=("AIR-03", "FAQ-Q01", "AIR-04", "FAQ-Q04", "AIR-02"),
            typed=False,
        )
        assert "contrast" in r.failed


# --------------------------------------------------------------------------- polarity guard


class TestStatementPolarity:
    @pytest.mark.parametrize(
        ("sentence", "refs"),
        [
            # FAQ question line asserts nothing; the answer line is the negated statement.
            (
                "No, you cannot take Uber Black or a premium rideshare for general use "
                "[T&E FAQ §FAQ-Q45].",
                ("FAQ-Q45",),
            ),
            (
                "No, minibar and in-room movie fees are not reimbursable [T&E FAQ §FAQ-Q17].",
                ("FAQ-Q17",),
            ),
            # "no more than 3 times" is a bound, not a negated statement.
            (
                "You may file a signed missing-receipt affidavit up to $75 [T&E Policy §EXP-04].",
                ("EXP-04",),
            ),
            # A permission and a prohibition joined by "but" / "while": two statements.
            (
                "An international roaming plan or local eSIM is also reimbursable, but personal "
                "overages are not [T&E FAQ §FAQ-Q48].",
                ("FAQ-Q48",),
            ),
            (
                "You should leave the original-currency amount on the line and not pre-convert "
                "it [How-To §HOW-26].",
                ("HOW-26",),
            ),
            # Closest evidence statement wins a tie: "only personal-card expenses are
            # reimbursed", not "corporate-card expenses ... (no personal reimbursement)".
            ("Only personal-card expenses are reimbursed [T&E FAQ §FAQ-Q37].", ("FAQ-Q37",)),
        ],
    )
    def test_faithful_statement_passes(self, sentence: str, refs: tuple[str, ...]) -> None:
        r = _assess(sentence, refs, retrieved=refs)
        assert "polarity" not in r.failed, r

    @pytest.mark.parametrize(
        ("sentence", "refs"),
        [
            ("Premium tiers like Uber Black are reimbursable [T&E FAQ §FAQ-Q45].", ("FAQ-Q45",)),
            ("Personal overages are reimbursable [T&E FAQ §FAQ-Q48].", ("FAQ-Q48",)),
            ("Minibar and movie fees are reimbursable [T&E Policy §LOD-04].", ("LOD-04",)),
            (
                "First class is reimbursable for flights > 10 hours [T&E Policy §AIR-03].",
                ("AIR-03",),
            ),
            (
                "An eSIM is reimbursable, and personal overages are reimbursable too "
                "[T&E FAQ §FAQ-Q48].",
                ("FAQ-Q48",),
            ),
        ],
    )
    def test_polarity_flip_is_still_caught(self, sentence: str, refs: tuple[str, ...]) -> None:
        r = _assess(sentence, refs, retrieved=refs)
        assert not r.supported, r

    def test_more_than_bound_still_checked_by_direction(self) -> None:
        r = _assess(
            "A missing-receipt affidavit may be filed more than 3 times per quarter "
            "[T&E Policy §EXP-04].",
            ("EXP-04",),
        )
        assert "direction" in r.failed


# ------------------------------------------------------------------------------ agent harness


class _ScriptedProvider(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(text=text, model="stub")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


@pytest.fixture(autouse=True)
def _novita_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RERANK_PROVIDER", "novita")
    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _run_calibrated(question: str, ref: str, shown: str, draft: str) -> tuple[Any, list[Any]]:
    """PolicyAgent on the CALIBRATED path: one retrieved clause shown with its whole section."""
    retrieval = GroundedRetrieval(
        query=question,
        status=GroundingStatus.grounded,
        clauses=(
            GroundedClause(
                clause_ref=ref, chunk_text=shown, similarity=0.4, document_type="policy"
            ),
        ),
        citations=(_cit(ref),),
        confidence=0.2,
        reason=None,
        threshold=0.20,
        confidence_source="calibrated",
        tau_star=0.039,
    )
    records: list[Any] = []

    def _log(_session: Any, *, context: Any, record: Any) -> MagicMock:
        records.append(record)
        return MagicMock()

    agent = PolicyAgent(provider=_ScriptedProvider(draft, "SUPPORTED\nFollows from the clauses."))
    ctx = AgentContext(
        organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
    )
    with (
        patch("ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=retrieval),
        patch(
            "ai_agents.agents.policy_agent.write_decision_log",
            side_effect=_log,
        ),
    ):
        result = agent.handle(
            Task(context_type="policy_qa", payload={"question": question}),
            RoutingDecision(
                target_agent=AiAgent.policy,
                route=AiRoute.llm,
                task_type="policy_grounded_lookup",
                confidence=0.95,
                rationale="t",
            ),
            context=ctx,
            session=MagicMock(),
        )
    return result, records


# ------------------------------------------------------------------- adversarial hardening
#
# Second review of the false-block fixes.  Each case below was GROUNDED / supported before the
# hardening and is pinned here:
#   * a bare yes/no verdict gained a citation through trailing reattachment ("Yes. [§AIR-01]
#     Economy ...") or carried one of its own ("Yes, you can. [§AIR-03]") and was grounded on
#     the calibrated path although the verdict answers a premise no clause can verify;
#   * a cited POLICY clause that merely names another clause ("CARD-03 ... within the EXP-01
#     deadline", "CONF-02 ... (see LOD-02)") laundered that clause's wording past the contrast
#     guard — only a derivative unit (FAQ / How-To / Reference) restates the rules it names;
#   * one stray final marker turned a leading-style draft into a trailing one, shifting every
#     citation a sentence back (and a pure trailing draft of 3+ sentences must not be read as
#     leading — the OPENING decides the style, not a count of marker positions);
#   * on the calibrated path a cited sentence that states nothing beyond verdict words ("You
#     can.", "It is permitted.") or beyond the question's own words ("Yes, you can fly.") is a
#     verdict, whatever its first word;
#   * an FAQ unit's QUESTION line anchored a "Yes" that its answer line contradicts.

_BUSINESS_Q = "Can I fly business class on a 3-hour flight?"
_AIR_SECTION = ("AIR-01", "AIR-02", "AIR-03")


def _support(cited_text: str, refs: tuple[str, ...], question: str, *, strict: bool) -> Any:
    from ai_agents.rag.grounding import check_entailment

    cited = extract_cited_sentences(cited_text, tuple(_cit(r) for r in refs))
    return check_entailment(cited, threshold=_THRESHOLD, question=question, strict_verdicts=strict)


class TestBareVerdict:
    @pytest.mark.parametrize(
        "draft",
        [
            "Yes. [T&E Policy §AIR-01] Economy is the standard for flights under 6 hours. "
            "[T&E Policy §AIR-01]",
            "Yes, you can. [T&E Policy §AIR-03]",
            "Yes [T&E Policy §AIR-01]. Economy is the standard for flights under 6 hours "
            "[T&E Policy §AIR-01].",
            "Yes, that is allowed [T&E Policy §AIR-03].",
        ],
        ids=["trailing-yes", "yes-you-can", "inline-yes", "yes-allowed"],
    )
    def test_verdict_is_never_grounded_on_the_calibrated_path(self, draft: str) -> None:
        result, records = _run_calibrated(_BUSINESS_Q, "AIR-01", _section(*_AIR_SECTION), draft)
        assert result.output["answer_type"] != "grounded", records[0].output_summary
        assert result.output["asserted"] is False
        # Never shown: at most the verified statement survives, without the verdict.
        assert not (result.output["answer"] or "").startswith("Yes")

    def test_trailing_reattachment_never_cites_a_verdict(self) -> None:
        parsed = _parse(
            "Yes. [T&E Policy §AIR-01] Economy is the standard for flights under 6 hours. "
            "[T&E Policy §AIR-01]",
            ("AIR-01",),
        )
        assert parsed[0] == ("Yes.", [])
        # A verdict opening reads as leading style: the marker after "Yes." opens "Economy".
        assert parsed[1][1] == ["AIR-01"] and "Economy is the standard" in parsed[1][0]
        assert len(parsed) == 2

    def test_verdict_with_its_own_citation_fails_alone_on_every_path(self) -> None:
        for strict in (False, True):
            er = _support(
                "Yes, you can [T&E Policy §AIR-03].", ("AIR-03",), _BUSINESS_Q, strict=strict
            )
            assert not er.entailed
            assert er.sentences[0].substantive is False

    def test_calibrated_path_fails_an_agreeing_verdict(self) -> None:
        draft = (
            "Yes [T&E Policy §AIR-01]. Economy is the standard for flights under 6 hours "
            "[T&E Policy §AIR-01]."
        )
        strict = _support(draft, ("AIR-01",), _BUSINESS_Q, strict=True)
        assert strict.sentences[0].failed == ("verdict",)
        assert strict.sentences[1].supported
        # Cosine path: unchanged — an agreeing bare verdict is still read with its statement.
        assert _support(draft, ("AIR-01",), _BUSINESS_Q, strict=False).entailed

    def test_substantive_verdict_answer_still_grounds(self) -> None:
        draft = (
            "No, business class is permitted only for flights > 10 hours, with VP approval "
            "[T&E Policy §AIR-03]. Economy is the standard for flights under 6 hours "
            "[T&E Policy §AIR-01]."
        )
        result, records = _run_calibrated(_BUSINESS_Q, "AIR-01", _section(*_AIR_SECTION), draft)
        assert result.output["answer_type"] == "grounded", records[0].output_summary

    @pytest.mark.parametrize(
        ("text", "verdict"),
        [
            ("Yes.", True),
            ("No, you cannot [T&E Policy §AIR-03].", True),
            ("Yes, that is allowed.", True),
            ("No fees apply to standard seat selection.", False),
            ("No receipts are needed for per diem days.", False),
            ("Yes, economy is the standard.", False),
        ],
    )
    def test_verdict_only_detection(self, text: str, verdict: bool) -> None:
        from ai_agents.rag.support import is_verdict_only

        assert is_verdict_only(text) is verdict


class TestPolicyCrossReference:
    def test_card_clause_does_not_launder_the_deadline_rule(self) -> None:
        # CARD-03 "... within the EXP-01 deadline": a cross-reference, not a restatement — the
        # VP approval is EXP-01's (late reports), borrowed under a CARD-03 citation.
        r = _assess(
            "Reconcile all card transactions in an expense report; reports require VP approval "
            "[T&E Policy §CARD-03].",
            ("CARD-03",),
            retrieved=("CARD-03", "EXP-01"),
        )
        assert not r.supported
        assert "contrast" in r.failed, r

    def test_conference_clause_does_not_launder_the_exception_approver(self) -> None:
        r = _assess(
            "Conference-hotel rates above standard caps are allowed with manager approval and "
            "justification [T&E Policy §CONF-02].",
            ("CONF-02",),
            retrieved=("CONF-02", "LOD-02"),
        )
        assert not r.supported
        assert "contrast" in r.failed, r

    def test_cited_derivative_still_restates_the_rule_it_names(self) -> None:
        r = _assess(
            "One checked bag and standard seat selection are reimbursable [T&E FAQ §FAQ-Q07].",
            ("FAQ-Q07",),
            retrieved=("FAQ-Q07", "AIR-07", "AIR-01"),
        )
        assert r.supported, r


class TestMarkerStyle:
    def test_one_stray_final_marker_does_not_flip_a_leading_draft(self) -> None:
        parsed = _parse(
            "Intro. [T&E Policy §AIR-01] A is stated. [T&E Policy §AIR-02] B is stated. "
            "[T&E Policy §AIR-03]",
            ("AIR-01", "AIR-02", "AIR-03"),
        )
        assert parsed[0] == ("Intro.", [])
        assert parsed[1][1] == ["AIR-01"]
        assert parsed[2][1][0] == "AIR-02"
        assert set(parsed[2][1]) <= {"AIR-02", "AIR-03"}
        assert len(parsed) == 3

    def test_two_sentence_trailing_draft_still_reattaches(self) -> None:
        parsed = _parse(
            "Economy is the standard for short flights. [T&E Policy §AIR-01] Business class "
            "needs VP approval. [T&E Policy §AIR-03]",
            ("AIR-01", "AIR-03"),
        )
        assert [refs for _, refs in parsed] == [["AIR-01"], ["AIR-03"]]

    def test_three_sentence_trailing_draft_reattaches_every_citation(self) -> None:
        parsed = _parse(
            "Economy is the standard for short flights. [T&E Policy §AIR-01] Premium economy "
            "needs manager approval. [T&E Policy §AIR-02] Business class needs VP approval. "
            "[T&E Policy §AIR-03]",
            ("AIR-01", "AIR-02", "AIR-03"),
        )
        assert [refs for _, refs in parsed] == [["AIR-01"], ["AIR-02"], ["AIR-03"]]
        assert parsed[0][0].startswith("Economy")

    def test_four_sentence_trailing_draft_reattaches_every_citation(self) -> None:
        parsed = _parse(
            "Economy is the standard for short flights. [T&E Policy §AIR-01] Premium economy "
            "needs manager approval. [T&E Policy §AIR-02] Business class needs VP approval. "
            "[T&E Policy §AIR-03] Every trip needs Pre-Trip Approval. [T&E Policy §PTA-01]",
            ("AIR-01", "AIR-02", "AIR-03", "PTA-01"),
        )
        assert [refs for _, refs in parsed] == [["AIR-01"], ["AIR-02"], ["AIR-03"], ["PTA-01"]]

    def test_leading_draft_without_a_stray_marker_is_unchanged(self) -> None:
        parsed = _parse(
            "Intro. [T&E Policy §AIR-01] A is stated. [T&E Policy §AIR-02] B is stated.",
            ("AIR-01", "AIR-02"),
        )
        assert [refs for _, refs in parsed] == [[], ["AIR-01"], ["AIR-02"]]
        parsed = _parse(
            "A is stated [T&E Policy §AIR-01]. Note. [T&E Policy §AIR-02] B is stated.",
            ("AIR-01", "AIR-02"),
        )
        assert [refs for _, refs in parsed] == [["AIR-01"], [], ["AIR-02"]]


class TestStrictVerdict:
    """Calibrated path: a cited sentence with no claim of its own is a verdict (``verdict``)."""

    @pytest.mark.parametrize(
        "draft",
        [
            "You can [T&E Policy §AIR-03].",
            "It is permitted [T&E Policy §AIR-03].",
            "That is allowed [T&E Policy §AIR-03].",
            "Business class needs VP approval [T&E Policy §AIR-03]. You can [T&E Policy §AIR-03].",
            "Yes, you can fly [T&E Policy §AIR-03].",
            "Yes, you can [T&E Policy §AIR-03] fly.",
            "Yes, you can book that flight [T&E Policy §AIR-03].",
        ],
        ids=[
            "you-can",
            "it-is-permitted",
            "that-is-allowed",
            "claim-then-you-can",
            "echo-fly",
            "echo-inline-marker",
            "echo-book-flight",
        ],
    )
    def test_claimless_sentence_is_never_grounded(self, draft: str) -> None:
        result, records = _run_calibrated(_BUSINESS_Q, "AIR-03", _section(*_AIR_SECTION), draft)
        assert result.output["answer_type"] != "grounded", records[0].output_summary
        assert result.output["asserted"] is False

    @pytest.mark.parametrize(
        "sentence",
        [
            "You can [T&E Policy §AIR-03].",
            "It is permitted [T&E Policy §AIR-03].",
            "That is allowed [T&E Policy §AIR-03].",
            "Yes, you can fly [T&E Policy §AIR-03].",
            "Yes, you can book that flight [T&E Policy §AIR-03].",
        ],
    )
    def test_claimless_sentence_fails_with_the_verdict_code(self, sentence: str) -> None:
        er = _support(sentence, ("AIR-03",), _BUSINESS_Q, strict=True)
        assert not er.entailed
        assert "verdict" in er.sentences[0].failed, er.sentences[0]
        assert er.sentences[0].substantive is False

    def test_cosine_path_is_unchanged(self) -> None:
        # Not strict: "It is permitted" keeps its base reading (substantive, no verdict code).
        er = _support(
            "It is permitted [T&E Policy §AIR-03].", ("AIR-03",), _BUSINESS_Q, strict=False
        )
        assert "verdict" not in er.sentences[0].failed

    def test_echo_of_the_whole_question_subject_is_left_to_the_other_guards(self) -> None:
        # Names everything asked about, so anchor / number / polarity can check it: not a
        # verdict (narrowed after replay: whole-subject "No, X is not reimbursable" answers).
        er = _support(
            "No, minibar and in-room movie fees are not reimbursable [T&E FAQ §FAQ-Q17].",
            ("FAQ-Q17",),
            "Can I expense the minibar or an in-room movie?",
            strict=True,
        )
        assert er.entailed, er.sentences

    def test_sentence_adding_a_backed_figure_still_grounds(self) -> None:
        draft = (
            "No, business class is permitted only for flights over 10 hours [T&E Policy §AIR-03]."
        )
        er = _support(draft, ("AIR-03",), _BUSINESS_Q, strict=True)
        assert er.entailed, er.sentences
        result, records = _run_calibrated(_BUSINESS_Q, "AIR-03", _section(*_AIR_SECTION), draft)
        assert result.output["answer_type"] == "grounded", records[0].output_summary


class TestQuestionLineEcho:
    def test_yes_echoing_an_faq_question_line_is_unanchored(self) -> None:
        r = _assess(
            "Yes, you can take Uber Black [T&E FAQ §FAQ-Q45].",
            ("FAQ-Q45",),
            question="Can I take Uber Black?",
            retrieved=("FAQ-Q45",),
        )
        assert not r.supported
        assert "unanchored" in r.failed, r

    def test_faithful_no_to_the_same_question_passes(self) -> None:
        r = _assess(
            "No, you cannot take Uber Black; premium tiers are not reimbursable "
            "[T&E FAQ §FAQ-Q45].",
            ("FAQ-Q45",),
            question="Can I take Uber Black?",
            retrieved=("FAQ-Q45",),
        )
        assert r.supported, r
