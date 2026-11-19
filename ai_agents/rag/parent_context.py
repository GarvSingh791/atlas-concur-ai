"""Parent-document context resolver (R0-T3).

Retrieval embeds and ranks the SMALL unit (a policy clause / FAQ / procedure /
reference entry).  For generation we can hand the model the enclosing PARENT
context instead of the bare unit:

  - policy clause            -> the whole enclosing SECTION (all sibling clauses).
  - faq / howto / reference  -> the unit itself (already self-contained).

The parent group is identified by ``(policy_id, parent_section_ref)`` stamped at
embed time (``database/seeds/corpus.chunking``); this resolver assembles it from
the persisted ``policy_embeddings`` rows ONLY — no dependency on the corpus seed
package — so it works for any organization's index.  Pure read; the caller owns
the session.

R0 SCOPE NOTE: this resolver is built, unit-tested, and used by the eval harness
for generation context.  Wiring it into the LIVE ``PolicyAgent`` generation path
is deferred to R1 — it changes generator behavior, whereas R0 only measures the
current retrieval (the retrieval RANKING is unchanged either way).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.policy_embedding import PolicyEmbedding


def resolve_parent_context(
    session: Session, *, policy_id: uuid.UUID, parent_section_ref: str
) -> str:
    """Assemble the enclosing context for a ``(policy_id, parent_section_ref)`` group.

    Concatenates, in document order (``ordinal``), the ``chunk_text`` of every
    embedding sharing the group key.  Robust to multi-prefix sections (e.g.
    ``DATA-*`` + ``CONT-*`` in policy Section 20): all share one
    ``parent_section_ref`` and are ordered by ``ordinal``.  Returns the joined text
    (``""`` if the group is empty).
    """
    rows = (
        session.execute(
            select(PolicyEmbedding.chunk_text)
            .where(
                PolicyEmbedding.policy_id == policy_id,
                PolicyEmbedding.parent_section_ref == parent_section_ref,
            )
            .order_by(PolicyEmbedding.ordinal.asc(), PolicyEmbedding.clause_ref.asc())
        )
        .scalars()
        .all()
    )
    return "\n\n".join(rows)


def resolve_parent_context_for_embedding(session: Session, embedding_id: str | uuid.UUID) -> str:
    """Resolve the parent context for a single retrieved embedding row.

    Looks the row up by id and assembles its ``(policy_id, parent_section_ref)``
    group.  A row with no ``parent_section_ref`` (legacy / stub) has no parent
    grouping, so its own ``chunk_text`` is returned unchanged.  Returns ``""`` if the
    id is unknown.
    """
    eid = embedding_id if isinstance(embedding_id, uuid.UUID) else uuid.UUID(str(embedding_id))
    row = session.get(PolicyEmbedding, eid)
    if row is None:
        return ""
    if row.parent_section_ref is None:
        return row.chunk_text
    return resolve_parent_context(
        session, policy_id=row.policy_id, parent_section_ref=row.parent_section_ref
    )
