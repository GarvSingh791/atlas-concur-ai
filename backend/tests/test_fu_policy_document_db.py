"""Follow-up (UI polish) — the Policy page shows the CURRENT Helios policy.

Old behaviour: the "Policy document" pane rendered GET /policies'
``document_text`` — the legacy stub's original 8 sections — while the assistant
answers from the Helios T&E corpus policy.

Now GET /policies also returns ``document``: the organisation's current corpus
``policy`` document in sections with clause codes, read from the indexed corpus
units the idempotent reindex boot step keeps current.  Read-only, tenant-scoped,
retrieval content untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.modules.policy.document import policy_document
from backend.tests.eval.test_rag_corpus_eval import _seed_corpus_for_org
from config.constants.embeddings import EMBEDDING_DIMENSION
from database.models.policy import PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds.corpus.policy import DOCUMENT as HELIOS

pytestmark = pytest.mark.db

URL = "/api/v1/policies"


def _unit(db: Session, policy: Any, ordinal: int, section: str, ref: str, text: str) -> None:
    db.add(
        PolicyEmbedding(
            policy_id=policy.id,
            chunk_text=text,
            clause_ref=ref,
            section=section,
            parent_section_ref=section,
            ordinal=ordinal,
            embedding=[0.0] * EMBEDDING_DIMENSION,
        )
    )


def test_policies_carries_the_current_corpus_policy_in_sections(
    db_session: Session, org_a: Any, employee_client: TestClient
) -> None:
    f.make_policy(db_session, org=org_a, is_active=True)  # the legacy stub
    _seed_corpus_for_org(db_session, org_a)

    r = employee_client.get(URL)
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["title"] == HELIOS.title
    assert doc["heading"].startswith("GLOBAL TRAVEL & EXPENSE")
    clauses = [c for s in doc["sections"] for c in s["clauses"]]
    # Every current unit, in document order, with its code and pristine text.
    assert [c["clause_ref"] for c in clauses] == [u.id for u in HELIOS.units]
    assert [c["text"] for c in clauses] == [u.text.strip() for u in HELIOS.units]
    # Sections in order, each once, far more than the stub's 8.
    titles = [s["title"] for s in doc["sections"]]
    assert titles == list(dict.fromkeys(u.section for u in HELIOS.units))
    assert len(titles) > 8
    assert {"AIR-04", "DEF-09", "MIL-01"} <= {c["clause_ref"] for c in clauses}


def test_document_is_tenant_scoped_and_policy_type_only(
    db_session: Session, org_a: Any, org_b: Any
) -> None:
    mine = f.make_policy(
        db_session,
        org=org_a,
        is_active=True,
        name="Mine",
        document_type=PolicyDocumentType.policy,
        document_text="HEAD\n\nbody",
    )
    faq = f.make_policy(
        db_session, org=org_a, is_active=True, name="FAQ", document_type=PolicyDocumentType.faq
    )
    theirs = f.make_policy(
        db_session,
        org=org_b,
        is_active=True,
        name="Theirs",
        document_type=PolicyDocumentType.policy,
    )
    _unit(db_session, mine, 1, "Section 2 — Air", "AIR-01", "AIR-01 Class: economy.")
    _unit(db_session, mine, 0, "Section 1 — General", "GEN-01", "GEN-01 Purpose: x.")
    _unit(db_session, mine, 2, "Section 2 — Air", "AIR-02", "AIR-02 Booking: y.")
    _unit(db_session, faq, 0, "FAQ", "FAQ-Q01", "not policy")
    _unit(db_session, theirs, 0, "Secret", "SEC-01", "other tenant")
    db_session.flush()

    doc = policy_document(db_session, org_a.id)
    assert doc is not None and doc.title == "Mine" and doc.heading == "HEAD"
    assert [(s.title, [c.clause_ref for c in s.clauses]) for s in doc.sections] == [
        ("Section 1 — General", ["GEN-01"]),
        ("Section 2 — Air", ["AIR-01", "AIR-02"]),
    ]
    other = policy_document(db_session, org_b.id)
    assert other is not None and [c.clause_ref for c in other.sections[0].clauses] == ["SEC-01"]


def test_no_corpus_policy_means_no_document_and_reads_change_nothing(
    db_session: Session, org_a: Any, employee_client: TestClient
) -> None:
    f.make_policy(db_session, org=org_a, is_active=True, document_text="stub text")
    before = db_session.scalar(sa.select(sa.func.count(PolicyEmbedding.id)))
    r = employee_client.get(URL)
    assert r.status_code == 200, r.text
    assert r.json()["document"] is None
    assert r.json()["document_text"] == "stub text"
    assert db_session.scalar(sa.select(sa.func.count(PolicyEmbedding.id))) == before
