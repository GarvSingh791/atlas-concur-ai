"""Helios Dynamics multi-document T&E corpus (R0-T2).

Four ORIGINAL synthetic documents, each seeded as one row in the ``policies``
table under the demo org with a ``document_type`` discriminator:

  (a) policy    — Helios Global Travel & Expense Policy (TEP-FY26), ~98 clauses.
  (b) faq       — Helios T&E FAQ, 50 Q&A.
  (c) howto      — Helios Expense & Travel How-To Guide, 35 procedures.
  (d) reference — Helios Receipt & Documentation Quick Reference, 25 entries.

All numeric figures come from ``figures.py`` (single source of truth), so the
four documents agree numerically by construction.  R0-T2 seeds the document
rows + full ``document_text`` only; chunking (R0-T3) and embedding (R0-T4) come
later and consume each document's ``units``.

``figures`` and ``_types`` are imported first so the document modules (which
import from them) resolve cleanly during package initialization.
"""

from __future__ import annotations

from database.seeds.corpus import figures
from database.seeds.corpus._types import CorpusDocument, CorpusUnit
from database.seeds.corpus.faq import DOCUMENT as FAQ
from database.seeds.corpus.howto import DOCUMENT as HOWTO
from database.seeds.corpus.policy import DOCUMENT as POLICY
from database.seeds.corpus.reference import DOCUMENT as REFERENCE

# Seed/registry order: authoritative policy first, then companions.
ALL_DOCUMENTS: tuple[CorpusDocument, ...] = (POLICY, FAQ, HOWTO, REFERENCE)

__all__ = [
    "ALL_DOCUMENTS",
    "CorpusDocument",
    "CorpusUnit",
    "FAQ",
    "HOWTO",
    "POLICY",
    "REFERENCE",
    "figures",
]
