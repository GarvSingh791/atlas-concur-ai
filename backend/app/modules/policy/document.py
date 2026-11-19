"""The policy document as the Policy page shows it (follow-up, UI polish).

GET /policies used to show only the legacy single-policy stub's text — the
original 8 sections — while the assistant answers from the Helios T&E policy
(the corpus ``policy`` document).  ``policy_document_sections`` reads the
CURRENT corpus policy document for the caller's organisation: its sections and
clauses (code + text) in document order, from the indexed corpus units that the
idempotent corpus reindex boot step (``database.seeds.reindex_policy_corpus``)
keeps in step with the code on every deploy.

Read-only.  Tenant-scoped (the document row is filtered by organisation; the
units are joined through it).  Retrieval content is untouched — this only reads
``policy_embeddings.chunk_text`` / ``clause_ref`` / ``section`` / ``ordinal``.
No corpus document or no indexed units → no sections (the page falls back to the
stored document text).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding


class PolicyDocumentClause(BaseModel):
    clause_ref: str
    text: str


class PolicyDocumentSection(BaseModel):
    title: str
    clauses: list[PolicyDocumentClause]


class PolicyDocument(BaseModel):
    title: str
    heading: str | None
    sections: list[PolicyDocumentSection]


def policy_document(session: Session, organization_id: uuid.UUID) -> PolicyDocument | None:
    """The organisation's current corpus policy document, sectioned; None if absent."""
    doc = session.scalars(
        sa.select(Policy)
        .where(
            Policy.organization_id == organization_id,
            Policy.document_type == PolicyDocumentType.policy,
            Policy.is_active.is_(True),
        )
        .order_by(Policy.version.desc(), Policy.created_at.desc(), Policy.id)
        .limit(1)
    ).first()
    if doc is None:
        return None
    rows = session.execute(
        sa.select(PolicyEmbedding.section, PolicyEmbedding.clause_ref, PolicyEmbedding.chunk_text)
        .join(Policy, Policy.id == PolicyEmbedding.policy_id)
        .where(Policy.id == doc.id, Policy.organization_id == organization_id)
        .order_by(PolicyEmbedding.ordinal.asc().nulls_last(), PolicyEmbedding.id)
    ).all()
    sections: list[PolicyDocumentSection] = []
    for section, clause_ref, text in rows:
        if not clause_ref or not text:
            continue
        title = (section or "").strip() or "General"
        if not sections or sections[-1].title != title:
            sections.append(PolicyDocumentSection(title=title, clauses=[]))
        sections[-1].clauses.append(PolicyDocumentClause(clause_ref=clause_ref, text=text.strip()))
    heading = (doc.document_text or "").split("\n\n", 1)[0].strip() or None
    return PolicyDocument(title=doc.name, heading=heading, sections=sections)
