"""Hermetic tests for the grounding / citation contract (FR-A4).

No database, no network.  All tests run in hermetic CI.
"""

from __future__ import annotations

from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    Citation,
    CitationKind,
    GroundedResult,
    validate_grounding,
)


def _make_citation(ref: str = "rule-001") -> Citation:
    return Citation(kind=CitationKind.clause_ref, ref=ref, detail="max $500 per night")


def _make_result(
    *,
    confidence: float,
    citations: tuple[Citation, ...] = (),
    asserted: bool = False,
) -> GroundedResult:
    return GroundedResult(
        claim="Hotel rate within policy",
        value=True,
        citations=citations,
        confidence=confidence,
        asserted=asserted,
    )


class TestValidateGrounding:
    def test_cited_above_threshold_becomes_asserted(self) -> None:
        result = _make_result(
            confidence=DEFAULT_CITATION_THRESHOLD,
            citations=(_make_citation(),),
        )
        validated = validate_grounding(result)
        assert validated.asserted is True

    def test_cited_well_above_threshold_becomes_asserted(self) -> None:
        result = _make_result(confidence=0.95, citations=(_make_citation(),))
        validated = validate_grounding(result)
        assert validated.asserted is True

    def test_zero_citations_not_asserted_regardless_of_confidence(self) -> None:
        result = _make_result(confidence=0.99, citations=())
        validated = validate_grounding(result)
        assert validated.asserted is False

    def test_below_threshold_not_asserted_even_with_citations(self) -> None:
        result = _make_result(
            confidence=DEFAULT_CITATION_THRESHOLD - 0.01,
            citations=(_make_citation(),),
        )
        validated = validate_grounding(result)
        assert validated.asserted is False

    def test_zero_confidence_zero_citations_not_asserted(self) -> None:
        result = _make_result(confidence=0.0, citations=())
        validated = validate_grounding(result)
        assert validated.asserted is False

    def test_custom_threshold_respected(self) -> None:
        result = _make_result(confidence=0.5, citations=(_make_citation(),))
        assert validate_grounding(result, threshold=0.6).asserted is False
        assert validate_grounding(result, threshold=0.5).asserted is True

    def test_validate_does_not_raise_on_uncited_result(self) -> None:
        result = _make_result(confidence=0.0)
        validated = validate_grounding(result)
        assert validated.asserted is False

    def test_other_fields_preserved_on_asserted(self) -> None:
        citation = _make_citation("rule-42")
        result = _make_result(confidence=0.9, citations=(citation,))
        validated = validate_grounding(result)
        assert validated.claim == result.claim
        assert validated.value == result.value
        assert validated.citations == (citation,)
        assert validated.confidence == 0.9

    def test_other_fields_preserved_on_not_asserted(self) -> None:
        result = _make_result(confidence=0.1, citations=())
        validated = validate_grounding(result)
        assert validated.claim == result.claim
        assert validated.confidence == 0.1


class TestGroundedResultToDict:
    def test_to_dict_shape_matches_jsonb_convention(self) -> None:
        citation = Citation(
            kind=CitationKind.clause_ref,
            ref="rule-001",
            detail="$500 hotel cap",
            source="2025 Travel Policy",
        )
        result = GroundedResult(
            claim="Hotel within policy",
            value=True,
            citations=(citation,),
            confidence=0.9,
            asserted=True,
        )
        d = result.to_dict()
        assert d["asserted"] is True
        assert d["confidence"] == 0.9
        assert len(d["citations"]) == 1
        c = d["citations"][0]
        assert c["kind"] == "clause_ref"
        assert c["ref"] == "rule-001"
        assert c["detail"] == "$500 hotel cap"
        assert c["source"] == "2025 Travel Policy"

    def test_to_dict_citation_without_optional_fields(self) -> None:
        citation = Citation(kind=CitationKind.tool_result, ref="inv-search-001")
        result = GroundedResult(
            claim="Flight available",
            value={"flight_id": "UA123"},
            citations=(citation,),
            confidence=0.85,
            asserted=True,
        )
        d = result.to_dict()
        c = d["citations"][0]
        assert c == {"kind": "tool_result", "ref": "inv-search-001"}
        assert "detail" not in c
        assert "source" not in c

    def test_to_dict_empty_citations(self) -> None:
        result = GroundedResult(
            claim="Unknown",
            value=None,
            citations=(),
            confidence=0.3,
            asserted=False,
        )
        d = result.to_dict()
        assert d["citations"] == []
        assert d["asserted"] is False

    def test_multiple_citations_serialized_in_order(self) -> None:
        citations = (
            Citation(kind=CitationKind.clause_ref, ref="rule-001"),
            Citation(kind=CitationKind.source_field, ref="expense_report.amount"),
        )
        result = GroundedResult(
            claim="Over limit",
            value=True,
            citations=citations,
            confidence=0.92,
            asserted=True,
        )
        d = result.to_dict()
        assert [c["ref"] for c in d["citations"]] == ["rule-001", "expense_report.amount"]


class TestCitationKindValues:
    def test_all_kinds_have_expected_string_values(self) -> None:
        assert CitationKind.clause_ref == "clause_ref"
        assert CitationKind.source_field == "source_field"
        assert CitationKind.tool_result == "tool_result"
