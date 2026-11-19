"""R1-T1 lexical full-text index population for ``policy_embeddings.chunk_tsv``.

The SPARSE (lexical, BM25-flavored) leg of hybrid retrieval (R1-T2) ranks over a
Postgres ``tsvector`` built from each chunk's text + its unit id (``clause_ref``) +
``section`` + the parent DOCUMENT TITLE (``policies.name``).  The title lives on the
parent ``policies`` row (cross-table), so ``chunk_tsv`` cannot be a STORED GENERATED
column — it is populated here, server-side, from the same transaction that writes the
embeddings.  Idempotent (recomputed from the same source columns every run) and
org-scoped (touches only one tenant's corpus rows).

``english`` is the text-search configuration: it keeps numeric tokens ($300 -> 300),
splits hyphenated codes (AIR-03 -> air-03 / air / 03), and stems words (affidavit) — so
the exact-term probe passes.  The sparse retrieval query in
``ai_agents/tools/policy_retrieval.py`` MUST query with this SAME config (kept in sync
by the exact-term probe test).

This module has NO ``backend`` imports by design, so both the production seeder
(``database.seeds.seed_all``) and the eval harness can reuse it without pulling the
backend layer.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

#: Postgres regconfig for the lexical index AND the sparse retrieval query (must match).
TSV_REGCONFIG = "english"

# The 'english' regconfig is a hardcoded SQL literal (not a bind parameter) to avoid the
# `to_tsvector(text, text) does not exist` resolution trap; only :org is bound.
_POPULATE_SQL = sa.text(
    """
    UPDATE policy_embeddings AS pe
    SET chunk_tsv = to_tsvector(
        'english',
        coalesce(pe.context_prefix, '') || ' ' ||
        coalesce(pe.chunk_text, '') || ' ' ||
        coalesce(pe.clause_ref, '') || ' ' ||
        coalesce(pe.section, '') || ' ' ||
        coalesce(p.name, '')
    )
    FROM policies AS p
    WHERE pe.policy_id = p.id
      AND p.organization_id = :org
    """
)


def populate_chunk_tsv(session: Session, organization_id: uuid.UUID) -> None:
    """Recompute ``policy_embeddings.chunk_tsv`` for every chunk under *organization_id*.

    Run after the embeddings for an org are inserted/flushed, in the SAME transaction.
    """
    session.execute(_POPULATE_SQL, {"org": str(organization_id)})
