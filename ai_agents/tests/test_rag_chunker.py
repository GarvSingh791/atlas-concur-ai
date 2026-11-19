"""Hermetic tests for ai_agents/rag/chunker.py — no DB, no network.

Verifies:
  - chunk_policy_document(DOCUMENT_TEXT) yields exactly 8 chunks
  - Correct clause_ref ordering matches document order
  - Preamble before first [LABEL] is discarded
  - Determinism: two calls produce identical results
  - SUSTAIN chunk is present
  - clause_ref set covers all 7 RULES clause_refs; SUSTAIN has no rule
  - All chunks embedded via LocalEmbeddingsProvider produce 1536-dim unit-norm vectors
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from ai_agents.rag.chunker import PolicyChunk, chunk_policy_document
from database.seeds.policy_content import DOCUMENT_TEXT
from database.seeds.policy_content import RULES as _ALL_RULES
from database.seeds.spend_rules_content import SPEND_RULES
from services.embeddings_provider.local import LocalEmbeddingsProvider

# Product gap 2: the spend-cap / gift / mileage rules cite TEP-FY26 clause ids, not
# the stub document's clauses — the stub chunk <-> rule pairing covers the stub rules.
RULES = [r for r in _ALL_RULES if r not in SPEND_RULES]

_EXPECTED_CLAUSE_REFS = [
    "AIR-CABIN",
    "AIR-PREF",
    "HOTEL-CAP",
    "HOTEL-PREF",
    "MEALS-PD",
    "GROUND-CLASS",
    "RECEIPT-REQ",
    "SUSTAIN",
]


class TestChunkerBasic:
    def test_returns_exactly_8_chunks(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        actual_refs = [c.clause_ref for c in chunks]
        assert len(chunks) == 8, f"Expected 8 chunks, got {len(chunks)}: {actual_refs}"

    def test_clause_ref_order(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        actual = [c.clause_ref for c in chunks]
        assert actual == _EXPECTED_CLAUSE_REFS, f"Order mismatch: {actual}"

    def test_no_empty_clause_ref(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        for c in chunks:
            assert c.clause_ref, f"Empty clause_ref found in chunk: {c!r}"

    def test_no_empty_chunk_text(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        for c in chunks:
            assert c.chunk_text, f"Empty chunk_text for clause_ref={c.clause_ref!r}"

    def test_preamble_is_discarded(self) -> None:
        """Text before the first [LABEL] marker must not appear in any chunk."""
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        # The preamble contains the line "This policy governs all business travel"
        preamble_snippet = "This policy governs"
        for c in chunks:
            assert (
                preamble_snippet not in c.chunk_text
            ), f"Preamble text leaked into chunk {c.clause_ref!r}: {c.chunk_text[:100]!r}"

    def test_no_label_bracket_in_chunk_text(self) -> None:
        """The [LABEL] tag itself must not appear in chunk_text."""
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        for c in chunks:
            assert (
                f"[{c.clause_ref}]" not in c.chunk_text
            ), f"Clause ref tag appeared in chunk_text for {c.clause_ref!r}"

    def test_chunk_is_frozen_dataclass(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        chunk = chunks[0]
        assert isinstance(chunk, PolicyChunk)
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.clause_ref = "CHANGED"  # type: ignore[misc]


class TestChunkerDeterminism:
    def test_two_calls_identical(self) -> None:
        first = chunk_policy_document(DOCUMENT_TEXT)
        second = chunk_policy_document(DOCUMENT_TEXT)
        assert first == second, "chunk_policy_document is not deterministic"

    def test_chunk_text_stable_across_calls(self) -> None:
        first = {c.clause_ref: c.chunk_text for c in chunk_policy_document(DOCUMENT_TEXT)}
        second = {c.clause_ref: c.chunk_text for c in chunk_policy_document(DOCUMENT_TEXT)}
        assert first == second


class TestSustainClause:
    def test_sustain_present(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        refs = {c.clause_ref for c in chunks}
        assert "SUSTAIN" in refs

    def test_sustain_has_no_rule(self) -> None:
        """SUSTAIN is intentionally rule-less — verifies the CLAUSES > RULES design."""
        rule_refs = {r["clause_ref"] for r in RULES}
        assert "SUSTAIN" not in rule_refs, "SUSTAIN should NOT have a corresponding rule"

    def test_all_rule_clause_refs_present_in_chunks(self) -> None:
        """Every rule's source_clause_ref must have a corresponding chunk."""
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        chunk_refs = {c.clause_ref for c in chunks}
        rule_refs = {r["clause_ref"] for r in RULES}
        missing = rule_refs - chunk_refs
        assert not missing, f"Rule clause_refs with no matching chunk: {missing}"

    def test_chunk_refs_superset_of_rule_refs(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        chunk_refs = {c.clause_ref for c in chunks}
        rule_refs = {r["clause_ref"] for r in RULES}
        assert rule_refs.issubset(chunk_refs)
        # SUSTAIN adds the extra element not in rules
        assert len(chunk_refs) == len(rule_refs) + 1

    def test_spend_rules_cite_real_tep_fy26_clauses(self) -> None:
        from database.seeds.corpus import POLICY

        corpus_refs = {u.id for u in POLICY.units}
        assert {r["clause_ref"] for r in SPEND_RULES} <= corpus_refs


class TestLocalEmbedderIntegration:
    """Hermetic embed round-trip: verify dimension and unit norm."""

    def test_all_vectors_1536_dim(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        provider = LocalEmbeddingsProvider()
        vectors = provider.embed_texts([c.chunk_text for c in chunks])
        assert len(vectors) == len(chunks)
        for i, v in enumerate(vectors):
            assert (
                len(v) == 1536
            ), f"Vector for chunk[{i}] ({chunks[i].clause_ref}) has dim {len(v)}, expected 1536"

    def test_all_vectors_unit_normalized(self) -> None:
        chunks = chunk_policy_document(DOCUMENT_TEXT)
        provider = LocalEmbeddingsProvider()
        vectors = provider.embed_texts([c.chunk_text for c in chunks])
        for i, v in enumerate(vectors):
            norm = math.sqrt(sum(x * x for x in v))
            assert (
                abs(norm - 1.0) < 1e-6
            ), f"Vector for chunk[{i}] ({chunks[i].clause_ref}) L2 norm={norm:.8f}, expected ≈1.0"
