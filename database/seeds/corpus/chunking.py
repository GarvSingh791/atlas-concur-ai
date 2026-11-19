"""Structured per-unit chunker for the Helios multi-document corpus (R0-T3).

Each ``CorpusUnit`` becomes exactly one chunk (mirroring the legacy per-clause
granularity); the units are short and well under the embedding token budget, so
no sub-chunking is required.  Every chunk carries the within-document metadata
the RAG index and the parent-context resolver need:

  clause_ref          -- the unit's stable id (e.g. "AIR-01", "FAQ-Q01").
  chunk_text          -- the unit's rendered prose (what gets embedded).
  section             -- the unit's section / category label.
  parent_section_ref  -- the parent-context GROUP KEY: the enclosing SECTION for a
                         policy clause (siblings share it, so the resolver can
                         assemble the whole section); the unit's OWN id for a
                         self-contained faq / howto / reference unit (parent =
                         self).
  ordinal             -- the unit's position within its document, for stable
                         parent-context assembly order (robust to multi-prefix
                         sections such as DATA-* + CONT-* in policy Section 20).

Pure + deterministic (no DB, no network).  Lives in the seed/corpus layer — the
only layer that can see both the structured units and feed seed_all._embed_corpus.
"""

from __future__ import annotations

from dataclasses import dataclass

from database.seeds.corpus._types import CorpusDocument

# Only the authoritative policy document groups several units under one enclosing
# SECTION; the companion documents are authored so each unit is already the right
# retrieval granularity (a full Q&A / procedure / reference entry), so its parent
# context is itself.
_PARENT_IS_SECTION = "policy"


@dataclass(frozen=True)
class CorpusChunk:
    """One embeddable chunk of a corpus document (one per CorpusUnit)."""

    clause_ref: str
    chunk_text: str
    section: str
    parent_section_ref: str
    ordinal: int


def chunk_document(doc: CorpusDocument) -> list[CorpusChunk]:
    """Chunk *doc* into one ``CorpusChunk`` per unit, stamping R0-T3 metadata."""
    parent_is_section = doc.document_type == _PARENT_IS_SECTION
    return [
        CorpusChunk(
            clause_ref=unit.id,
            chunk_text=unit.text,
            section=unit.section,
            parent_section_ref=unit.section if parent_is_section else unit.id,
            ordinal=ordinal,
        )
        for ordinal, unit in enumerate(doc.units)
    ]
