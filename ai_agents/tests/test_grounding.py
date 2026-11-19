"""Hermetic tests for R5-T2 citation grounding (ai_agents.rag.grounding).

Pure functions — no DB, no LLM.  Covers citation_label for every document_type, the
extract_cited_sentences parser (valid / uncited / hallucinated-ref / multi-cite / boundary
robustness), and the CitedAnswer structure incl. JSON-safety of to_dict().
"""

from __future__ import annotations

import json

from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.rag.grounding import (
    CitedAnswer,
    CitedSentence,
    citation_label,
    extract_cited_sentences,
)
from ai_agents.rag.retrieval import GroundedClause


def _clause(clause_ref: str | None, document_type: str | None) -> GroundedClause:
    return GroundedClause(
        clause_ref=clause_ref, chunk_text="…", similarity=0.5, document_type=document_type
    )


def _cite(ref: str, source: str = "Atlas T&E Policy") -> Citation:
    return Citation(kind=CitationKind.clause_ref, ref=ref, detail="leaf text", source=source)


class TestCitationLabel:
    def test_all_four_document_types(self) -> None:
        assert citation_label(_clause("AIR-01", "policy")) == "[T&E Policy §AIR-01]"
        assert citation_label(_clause("FAQ-Q01", "faq")) == "[T&E FAQ §FAQ-Q01]"
        assert citation_label(_clause("HOW-08", "howto")) == "[How-To §HOW-08]"
        assert citation_label(_clause("REF-10", "reference")) == "[Reference §REF-10]"

    def test_none_document_type_falls_back_to_policy(self) -> None:
        assert citation_label(_clause("LOD-01", None)) == "[Policy §LOD-01]"

    def test_unknown_document_type_falls_back_to_policy(self) -> None:
        assert citation_label(_clause("X-01", "weird")) == "[Policy §X-01]"

    def test_missing_clause_ref_drops_section_suffix(self) -> None:
        assert citation_label(_clause(None, "policy")) == "[T&E Policy]"


class TestExtractCitedSentences:
    def test_single_valid_citation(self) -> None:
        cites = (_cite("AIR-01"),)
        ans = extract_cited_sentences("Book economy on short flights [T&E Policy §AIR-01].", cites)
        assert len(ans.sentences) == 1
        s = ans.sentences[0]
        assert s.citations[0].ref == "AIR-01"
        assert not ans.needs_review

    def test_uncited_sentence_is_flagged_not_dropped(self) -> None:
        ans = extract_cited_sentences("Business class is always fine.", (_cite("AIR-01"),))
        assert len(ans.sentences) == 1  # retained
        assert ans.sentences[0].citations == ()
        assert ans.needs_review

    def test_hallucinated_ref_is_rejected(self) -> None:
        # Marker shape is valid but ZZZ-99 is not an available citation → dropped.
        ans = extract_cited_sentences("It is fine [Made Up §ZZZ-99].", (_cite("AIR-01"),))
        assert ans.sentences[0].citations == ()
        assert ans.needs_review

    def test_multi_citation_single_sentence(self) -> None:
        cites = (_cite("AIR-01"), _cite("FAQ-Q01"))
        ans = extract_cited_sentences(
            "Premium economy is conditional [T&E Policy §AIR-01] [T&E FAQ §FAQ-Q01].", cites
        )
        assert len(ans.sentences) == 1
        refs = {c.ref for c in ans.sentences[0].citations}
        assert refs == {"AIR-01", "FAQ-Q01"}

    def test_two_sentences_split_on_boundary_after_marker(self) -> None:
        # The '.' after the [ …§LOD-01] marker must end sentence 1, not split mid-citation.
        cites = (_cite("LOD-01"), _cite("REF-10"))
        ans = extract_cited_sentences(
            "Book within the cap [T&E Policy §LOD-01]. See [Reference §REF-10] for details.", cites
        )
        assert len(ans.sentences) == 2
        assert ans.sentences[0].citations[0].ref == "LOD-01"
        assert ans.sentences[1].citations[0].ref == "REF-10"

    def test_numeric_ref_without_bracket_does_not_split(self) -> None:
        # '§4.2.1' internal dots and a bracket-less numeric ref must not create boundaries.
        ans = extract_cited_sentences("Per §4.2.1 the per diem is $79.", ())
        assert len(ans.sentences) == 1
        assert ans.sentences[0].citations == ()

    def test_full_text_preserves_answer_and_markers(self) -> None:
        raw = "Economy is required [T&E Policy §AIR-01]."
        ans = extract_cited_sentences(raw, (_cite("AIR-01"),))
        assert ans.full_text == raw

    def test_distinct_citations_dedupes_by_ref(self) -> None:
        cites = (_cite("AIR-01"),)
        ans = extract_cited_sentences("A [T&E Policy §AIR-01]. B [T&E Policy §AIR-01].", cites)
        assert len(ans.distinct_citations()) == 1

    def test_empty_answer_needs_review(self) -> None:
        ans = extract_cited_sentences("", (_cite("AIR-01"),))
        assert ans.sentences == ()
        assert ans.needs_review

    def test_nul_byte_input_does_not_raise(self) -> None:
        # A pre-existing \x00N\x00-shaped substring must be stripped, not collide with the mask
        # index (would IndexError) — this pure parser must never raise (reflect() contract).
        ans = extract_cited_sentences(
            "Weird \x005\x00 stuff [T&E Policy §AIR-01].", (_cite("AIR-01"),)
        )
        assert ans.sentences
        assert ans.sentences[0].citations[0].ref == "AIR-01"


class TestCitedAnswerStructure:
    def test_str_equals_full_text(self) -> None:
        ans = CitedAnswer((CitedSentence("Hello world.", (_cite("AIR-01"),)),))
        assert str(ans) == ans.full_text == "Hello world."

    def test_to_dict_is_json_serializable(self) -> None:
        ans = extract_cited_sentences(
            "Economy required [T&E Policy §AIR-01]. Business is not [T&E FAQ §FAQ-Q01].",
            (_cite("AIR-01"), _cite("FAQ-Q01")),
        )
        d = ans.to_dict()
        # The streaming path json.dumps(dict(output)); a CitedAnswer must survive that.
        dumped = json.dumps(d)
        assert '"full_text"' in dumped
        assert d["needs_review"] is False
        assert len(d["sentences"]) == 2
        assert d["sentences"][0]["citations"][0]["ref"] == "AIR-01"
