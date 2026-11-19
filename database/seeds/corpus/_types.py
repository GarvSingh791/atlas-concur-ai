"""Shared corpus document contract for the Helios Dynamics T&E corpus (R0-T2).

Each corpus module (policy / faq / howto / reference) exports a single
module-level ``DOCUMENT: CorpusDocument``.  ``database/seeds/corpus/__init__.py``
aggregates them into ``ALL_DOCUMENTS`` and the seed runner turns each into one
row in the ``policies`` table (demo org) with ``document_type`` + ``title``.

The per-unit structure (``CorpusUnit``) is retained here for downstream R0-T3
(chunking) and R0-T4 (embedding) to consume — R0-T2 itself only writes the
document row + full ``document_text`` (no chunking, no embeddings).
"""

from __future__ import annotations

from dataclasses import dataclass

# Allowed document_type values — MUST match the policy_document_type PG enum
# (migration 0008) and PolicyDocumentType (database/models/policy.py).
DOCUMENT_TYPES: tuple[str, ...] = ("policy", "faq", "howto", "reference")


@dataclass(frozen=True)
class CorpusUnit:
    """One retrievable unit of a corpus document.

    id       : stable, document-namespaced identifier (e.g. "AIR-01", "FAQ-Q01",
               "HOW-01", "REF-01").  Unique within its document.
    section  : section / category label the unit belongs to (grouping for R0-T3).
    text     : the rendered unit prose — this is the chunk text R0-T3/T4 will use,
               and what the consistency test greps for canonical figures.
    """

    id: str
    section: str
    text: str


@dataclass(frozen=True)
class CorpusDocument:
    """A single corpus document destined for one ``policies`` row.

    doc_id         : document-level stable id / namespace (e.g. "TEP-FY26").
    document_type  : one of DOCUMENT_TYPES → policies.document_type enum.
    title          : human title → policies.name (the document discriminator).
    version        : integer document version → policies.version.
    effective_from : ISO date string → policies.effective_from.
    header         : title block rendered at the top of document_text.
    units          : ordered units; each becomes a chunk in R0-T3/T4.
    """

    doc_id: str
    document_type: str
    title: str
    version: int
    effective_from: str
    header: str
    units: tuple[CorpusUnit, ...]

    def __post_init__(self) -> None:
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(
                f"{self.doc_id}: document_type {self.document_type!r} " f"not in {DOCUMENT_TYPES}"
            )
        ids = [u.id for u in self.units]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"{self.doc_id}: duplicate unit ids {dupes}")

    @property
    def document_text(self) -> str:
        """Full document prose: header followed by every unit's rendered text.

        This is what is stored in policies.document_text and what the consistency
        check greps; because each unit interpolates figures.py, the rendered text
        carries the canonical figures by construction.
        """
        body = "\n\n".join(u.text for u in self.units)
        return f"{self.header}\n\n{body}"

    @property
    def unit_count(self) -> int:
        return len(self.units)
