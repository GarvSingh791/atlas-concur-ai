"""Atlas full seed — single idempotent entrypoint for P3-T5.

Run after migrations:
  alembic upgrade head
  python -m database.seeds.seed_all

Dependency order (each step is idempotent):
  1. Identity     — RBAC catalog + org + three role users (via identity_seed)
  2. Policy       — legacy stub (document + 7 rules; RAG index retired in R0-T4) +
                    the 4-document Helios corpus chunked & embedded (219 units)
  3. Sample trip  — one past SFO→JFK trip + flight/hotel segments (anchor for grounding)
  4. Card txns    — ~3 simulated card swipes aligned to the sample trip
  5. Calendar     — 1-2 events aligned to the trip dates/location
  6. Receipts     — 3 pending receipts (ocr_status='pending', all AI fields NULL)
  7. MinIO upload — sample PNG images for each receipt key (lazy, self-skipping)

No migration 0007 is created.  No AI outputs are fabricated.  No embeddings provider
or storage_provider abstraction is used.  All credentials are hashed; dev plaintext
appears only as the INPUT to hash_password, never stored in the DB.

STORAGE_* env vars are read from os.environ directly (Settings ignores them with
extra='ignore').  The MinIO step is wrapped in try/except and skips gracefully if
boto3 or MinIO is unavailable — the DB rows (including image_url) always seed.
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Ensure repo root is on sys.path when run as __main__
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.core.database import get_sessionmaker  # noqa: E402
from backend.app.core.security import hash_password, verify_password  # noqa: E402
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType  # noqa: E402
from database.models.approval_step import ApprovalDecision, ApprovalStep  # noqa: E402
from database.models.calendar_event import CalendarEvent  # noqa: E402
from database.models.card_transaction import CardTransaction  # noqa: E402
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus  # noqa: E402
from database.models.expense_report import ExpenseReport, ExpenseReportStatus  # noqa: E402
from database.models.identity import Organization, RoleKey, User, UserStatus  # noqa: E402
from database.models.policy import Policy, PolicyDocumentType  # noqa: E402
from database.models.policy_embedding import PolicyEmbedding  # noqa: E402
from database.models.policy_rule import (  # noqa: E402
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus, Receipt  # noqa: E402
from database.models.trip import Trip, TripStatus  # noqa: E402
from database.models.trip_segment import SegmentType, TripSegment  # noqa: E402
from database.seeds.chunk_tsv import populate_chunk_tsv  # noqa: E402
from database.seeds.corpus import ALL_DOCUMENTS  # noqa: E402
from database.seeds.corpus import figures as F  # noqa: E402
from database.seeds.corpus.chunking import chunk_document  # noqa: E402
from database.seeds.corpus.contextual import context_prefix  # noqa: E402
from database.seeds.identity_seed import seed as seed_identity  # noqa: E402
from database.seeds.policy_content import (  # noqa: E402
    DOCUMENT_TEXT,
    ENTRY_RULES,
    POLICY_EFFECTIVE_FROM,
    POLICY_NAME,
    POLICY_VERSION,
    PROCESS_RULES,
    RULES,
)
from database.seeds.topup_demo_card_feed import DEMO_CARD_TRANSACTIONS  # noqa: E402

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from services.embeddings_provider.interface import EmbeddingsProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sample trip constants (grounding anchor — SFO → JFK, June 2026)
# ---------------------------------------------------------------------------

_TRIP_ORIGIN = "SFO"
_TRIP_DEST = "JFK"
_TRIP_START = date(2026, 6, 10)
_TRIP_END = date(2026, 6, 14)

# Receipt image keys — stable, deterministic, no binaries committed
_RECEIPT_KEYS: list[str] = [
    "receipts/seed/employee-airfare-001.png",
    "receipts/seed/employee-hotel-001.png",
    "receipts/seed/employee-meal-001.png",
]

# Minimal 1×1 white PNG (89 bytes) — valid PNG, tiny, no external files needed
_SAMPLE_PNG_BYTES: bytes = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Step 1b: Password resync (idempotent — only updates when hash is stale)
# ---------------------------------------------------------------------------


def _resync_user_passwords(session: Session) -> None:
    """Ensure all demo-org users have a password_hash matching the current
    SEED_DEFAULT_PASSWORD.  The org-guard in identity_seed skips rehashing on
    re-runs, so we correct any stale hashes here.  Idempotent: no-op when the
    hash is already correct.
    """
    from config.settings import get_settings  # noqa: PLC0415

    s = get_settings()
    pw = s.SEED_DEFAULT_PASSWORD
    demo_emails = [
        "employee@atlas-demo.internal",
        "manager@atlas-demo.internal",
        "finance@atlas-demo.internal",
    ]
    updated = 0
    for email in demo_emails:
        # Demo org only: since migration 0022 the same email may exist elsewhere.
        user = (
            session.query(User)
            .join(Organization, Organization.id == User.organization_id)
            .filter(Organization.name == "Atlas Demo Corp", User.email == email)
            .first()
        )
        if user is None:
            continue
        if not verify_password(pw, user.password_hash):
            user.password_hash = hash_password(pw)
            updated += 1
    if updated:
        session.flush()
        logger.info("Resynced %d user password hash(es) to current SEED_DEFAULT_PASSWORD.", updated)


# ---------------------------------------------------------------------------
# Step 2: Policy + rules + embeddings
# ---------------------------------------------------------------------------


def _seed_policy(session: Session, org: Organization, finance_admin: User) -> None:
    """Idempotent: skips if policy already exists for this (org, name, version)."""
    existing = (
        session.query(Policy)
        .filter(
            Policy.organization_id == org.id,
            Policy.name == POLICY_NAME,
            Policy.version == POLICY_VERSION,
        )
        .first()
    )
    if existing is not None:
        logger.info("Policy already seeded — skipping.")
        return

    effective_date = date.fromisoformat(POLICY_EFFECTIVE_FROM)
    policy = Policy(
        organization_id=org.id,
        name=POLICY_NAME,
        version=POLICY_VERSION,
        effective_from=effective_date,
        document_text=DOCUMENT_TEXT,
        is_active=True,
        created_by=finance_admin.id,
        updated_by=finance_admin.id,
    )
    session.add(policy)
    session.flush()  # populate policy.id

    # Rules
    # Product gap 2: the entry-level rules (GRND-01 / 03 / 05) and the process rules
    # (EXP-01, PTA-02, PTA-05, AIR-06, CARD-03) are seeded with the stub.
    for rule_def in (*RULES, *ENTRY_RULES, *PROCESS_RULES):
        rule = PolicyRule(
            policy_id=policy.id,
            rule_type=PolicyRuleType(rule_def["rule_type"]),
            applies_to=PolicyAppliesTo(rule_def["applies_to"]),
            definition=rule_def["definition"],
            source_clause_ref=rule_def["clause_ref"],
            status=PolicyRuleStatus.active,
            created_by=finance_admin.id,
            updated_by=finance_admin.id,
        )
        session.add(rule)

    # No embeddings here: the legacy stub's RAG index is RETIRED in R0-T4 — the
    # Helios corpus (_embed_corpus) serves retrieval.  The stub policy + its rules
    # are kept for the deterministic rule engine; HOTEL-CAP / MEALS-PD are derived
    # from the canonical corpus figures (Issue 6, migration 0015 reconciles older
    # databases).  Any stub embeddings left by a prior seed are deleted by
    # _retire_stub_embeddings.
    session.flush()
    logger.info(
        "Stub policy seeded: %d rules, 0 embeddings (RAG index retired in R0-T4).",
        len(RULES) + len(ENTRY_RULES) + len(PROCESS_RULES),
    )


def _seed_corpus(session: Session, org: Organization, finance_admin: User) -> None:
    """Seed the four-document Helios T&E corpus (R0-T2) as policies rows.

    Additive and non-breaking: each CorpusDocument becomes one ``policies`` row
    with ``document_type`` + ``title`` + full ``document_text``.  This does NOT
    touch the legacy single-policy stub (``_seed_policy``), which keeps serving
    retrieval until R0-T4 re-embeds the new corpus and retires the stub.

    Scope (R0-T2): document rows only — NO policy_rules, NO policy_embeddings.
    Chunking is R0-T3; embedding is R0-T4.

    Idempotent: guards on (organization_id, name, version), the same key as the
    uq_policies_organization_id_name_version constraint — re-running yields
    identical rows/counts with no duplicates.
    """
    seeded = 0
    refreshed = 0
    for doc in ALL_DOCUMENTS:
        existing = (
            session.query(Policy)
            .filter(
                Policy.organization_id == org.id,
                Policy.name == doc.title,
                Policy.version == doc.version,
            )
            .first()
        )
        if existing is not None:
            # Known limits (AIR-04 grades): a corpus edit (e.g. the traveller-grade
            # clauses) re-renders the text; keep the stored copy in step with the units
            # _embed_corpus indexes.  System-owned rows: only document_text changes.
            if existing.document_text != doc.document_text:
                existing.document_text = doc.document_text
                refreshed += 1
            continue
        session.add(
            Policy(
                organization_id=org.id,
                name=doc.title,
                version=doc.version,
                effective_from=date.fromisoformat(doc.effective_from),
                document_text=doc.document_text,
                document_type=PolicyDocumentType(doc.document_type),
                is_active=True,
                created_by=finance_admin.id,
                updated_by=finance_admin.id,
            )
        )
        seeded += 1

    if seeded or refreshed:
        session.flush()
    logger.info(
        "Corpus seeded: %d of %d documents (policy/faq/howto/reference); %d text(s) refreshed.",
        seeded,
        len(ALL_DOCUMENTS),
        refreshed,
    )


def _embed_corpus(
    session: Session,
    org: Organization,
    refresh_context: bool = False,
    embedder: EmbeddingsProvider | None = None,
) -> int:
    """Chunk + embed the four-document Helios corpus into policy_embeddings (R0-T4).

    For each corpus document (seeded as a ``policies`` row by ``_seed_corpus``):
    chunk it one-unit-per-chunk with R0-T3 metadata, embed each chunk with the
    config-selected embedder, and REPLACE the document's embeddings
    (delete-then-insert per ``policy_id``).  Idempotent: re-running yields identical
    per-document counts with no duplicates.  Returns the total chunk count.

    R4-T1 contextual prefixes: each chunk is situated by ``context_prefix`` (FORK-C
    dispatch on settings).  The prefix is fused into the EMBEDDING input
    (``f"{prefix}\\n\\n{chunk_text}"``) and persisted in ``context_prefix`` so the
    lexical tsvector can fuse it too; ``chunk_text`` stays PRISTINE (reranker +
    citations read it).  FORK-D persist-and-reuse: prior persisted prefixes are
    passed back as ``cached=...`` so a production LLM reindex is a no-op (no fresh
    calls).  ``refresh_context=True`` discards the cache and regenerates.  When
    ``RAG_CONTEXTUAL_PREFIXES`` is OFF, ``context_prefix`` returns ``""`` ->
    embed_input is the bare chunk_text and the stored prefix is NULL (byte-identical
    to R3).

    R4-T3 FORK-G: the embedder is config-selected via ``get_embeddings_provider()`` (or an
    explicit ``embedder`` override).  Under ``EMBEDDINGS_PROVIDER=local`` (CI/dev default)
    the factory returns ``LocalEmbeddingsProvider``, whose ``embed_texts([x])[0]`` is
    byte-identical to the legacy ``embed(x)`` — so the committed default re-seeds
    R3-byte-identically with zero external calls.  Production sets
    ``EMBEDDINGS_PROVIDER=openai`` to re-embed with the real model (query + doc matched).
    """
    from services.embeddings_provider.factory import get_embeddings_provider  # noqa: PLC0415

    provider = embedder or get_embeddings_provider()
    total = 0
    for doc in ALL_DOCUMENTS:
        policy = (
            session.query(Policy)
            .filter(
                Policy.organization_id == org.id,
                Policy.name == doc.title,
                Policy.version == doc.version,
            )
            .first()
        )
        if policy is None:
            logger.warning(
                "Corpus doc %s has no policies row (run _seed_corpus first) — skipping embed.",
                doc.doc_id,
            )
            continue
        chunks = chunk_document(doc)
        # FORK-D: capture prior persisted prefixes for reuse BEFORE the delete.
        existing = (
            session.query(PolicyEmbedding.clause_ref, PolicyEmbedding.context_prefix)
            .filter(PolicyEmbedding.policy_id == policy.id)
            .all()
        )
        prior = (
            {}
            if refresh_context
            else {r.clause_ref: r.context_prefix for r in existing if r.context_prefix}
        )
        # Idempotent transactional replace: clear this policy's rows, then re-insert.
        session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy.id).delete(
            synchronize_session=False
        )
        for chunk in chunks:
            prefix = context_prefix(
                document_title=doc.title,
                document_type=doc.document_type,
                section=chunk.section,
                clause_ref=chunk.clause_ref,
                document_text=doc.document_text,
                chunk_text=chunk.chunk_text,
                cached=prior.get(chunk.clause_ref),
            )
            embed_input = f"{prefix}\n\n{chunk.chunk_text}" if prefix else chunk.chunk_text
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=chunk.chunk_text,
                    context_prefix=(prefix or None),
                    clause_ref=chunk.clause_ref,
                    section=chunk.section,
                    parent_section_ref=chunk.parent_section_ref,
                    ordinal=chunk.ordinal,
                    embedding=provider.embed_texts([embed_input])[0],
                )
            )
        total += len(chunks)
    session.flush()
    # R1-T1: build the lexical full-text index (chunk_tsv) for this org's corpus rows in
    # the same transaction, from chunk_text + clause_ref + section + document title.
    populate_chunk_tsv(session, org.id)
    logger.info("Corpus embedded: %d chunks across %d documents.", total, len(ALL_DOCUMENTS))
    return total


def _retire_stub_embeddings(session: Session, org: Organization) -> int:
    """Physically delete the legacy stub policy's embeddings (R0-T4 retirement).

    Retrieval JOINs on organization_id only (no ``is_active`` filter), so retiring
    the stub from RAG requires DELETING its ``policy_embeddings`` rows — deactivating
    is not enough.  The stub ``policies`` row and its ``policy_rules`` are KEPT (the
    deterministic rule-eval path is out of scope).  Runs unconditionally so it also
    cleans up an already-seeded / staging database.  Returns the rows deleted.
    """
    stub = (
        session.query(Policy)
        .filter(
            Policy.organization_id == org.id,
            Policy.name == POLICY_NAME,
            Policy.version == POLICY_VERSION,
        )
        .first()
    )
    if stub is None:
        return 0
    deleted = (
        session.query(PolicyEmbedding)
        .filter(PolicyEmbedding.policy_id == stub.id)
        .delete(synchronize_session=False)
    )
    session.flush()
    if deleted:
        logger.info("Retired %d legacy stub embedding(s); stub policy + rules kept.", deleted)
    return deleted


# The seeded DRAFT rule (navigator draft dot + draft-activation flow).  Module-level so
# the rule-definition validator's regression test covers it (Issue 5).
CAR_CAP_DRAFT_RULE: dict[str, Any] = {
    "clause_ref": "CAR-CAP-DRAFT",
    "rule_type": "amount_cap",
    "applies_to": "car",
    "definition": {
        "currency": "USD",
        "per": "day",
        "tiers": [{"tier": "standard", "cap": 75}],
    },
}


def _seed_draft_rule(session: Session, org: Organization, finance_admin: User) -> None:
    """Seed one DRAFT rule so the policy navigator exercises draft status dots
    and the draft-activation flow. Idempotent: skips if any draft rule exists
    for the org's active policy."""
    policy = (
        session.query(Policy)
        .filter(
            Policy.organization_id == org.id,
            Policy.name == POLICY_NAME,
            Policy.version == POLICY_VERSION,
        )
        .first()
    )
    if policy is None:
        return

    existing_draft = (
        session.query(PolicyRule)
        .filter(
            PolicyRule.policy_id == policy.id,
            PolicyRule.status == PolicyRuleStatus.draft,
        )
        .first()
    )
    if existing_draft is not None:
        logger.info("Draft rule already seeded — skipping.")
        return

    draft = PolicyRule(
        policy_id=policy.id,
        rule_type=PolicyRuleType(CAR_CAP_DRAFT_RULE["rule_type"]),
        applies_to=PolicyAppliesTo(CAR_CAP_DRAFT_RULE["applies_to"]),
        definition=copy.deepcopy(CAR_CAP_DRAFT_RULE["definition"]),
        source_clause_ref=CAR_CAP_DRAFT_RULE["clause_ref"],
        status=PolicyRuleStatus.draft,
        created_by=finance_admin.id,
        updated_by=finance_admin.id,
    )
    session.add(draft)
    session.flush()
    logger.info("Draft policy rule seeded (car amount_cap).")


# ---------------------------------------------------------------------------
# Step 3: Sample trip + segments
# ---------------------------------------------------------------------------


def _seed_sample_trip(session: Session, org: Organization, employee: User) -> Trip:
    """Idempotent: skips if a trip with same (traveler, origin, dest, start_date) exists.

    Returns the existing or newly seeded trip (used for grounding rows).
    """
    existing = (
        session.query(Trip)
        .filter(
            Trip.traveler_id == employee.id,
            Trip.origin == _TRIP_ORIGIN,
            Trip.primary_destination == _TRIP_DEST,
            Trip.start_date == _TRIP_START,
        )
        .first()
    )
    if existing is not None:
        logger.info("Sample trip already seeded — skipping.")
        return existing

    trip = Trip(
        organization_id=org.id,
        traveler_id=employee.id,
        origin=_TRIP_ORIGIN,
        primary_destination=_TRIP_DEST,
        start_date=_TRIP_START,
        end_date=_TRIP_END,
        purpose="Q2 Engineering Summit",
        estimated_cost=Decimal("1850.00"),
        budget_amount=Decimal("2000.00"),
        currency="USD",
        status=TripStatus.completed,
        recommendation_context=None,  # no AI outputs
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(trip)
    session.flush()  # populate trip.id

    # Flight segment (outbound SFO→JFK)
    flight_seg = TripSegment(
        trip_id=trip.id,
        segment_type=SegmentType.flight,
        supplier="United Airlines",
        start_datetime=datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 6, 10, 15, 30, tzinfo=timezone.utc),
        location="New York, NY",
        amount=Decimal("320.00"),
        currency="USD",
        in_policy=None,  # policy-eval output — not fabricated
        policy_notes=None,  # policy-eval output — not fabricated
        details={
            "flight_number": "UA201",
            "cabin_class": "economy",
            "origin_airport": "SFO",
            "destination_airport": "JFK",
            "duration_hours": 5.5,
            "is_direct": True,
        },
    )
    session.add(flight_seg)

    # Hotel segment (4 nights in NYC)
    hotel_seg = TripSegment(
        trip_id=trip.id,
        segment_type=SegmentType.hotel,
        supplier="Marriott International",
        start_datetime=datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 6, 14, 11, 0, tzinfo=timezone.utc),
        location="New York, NY",
        amount=Decimal("956.00"),  # 4 nights × $239
        currency="USD",
        in_policy=None,  # policy-eval output — not fabricated
        policy_notes=None,
        details={
            "hotel_name": "New York Marriott Marquis",
            "room_type": "Standard",
            "nightly_rate_usd": 239.00,
            "check_in_date": "2026-06-10",
            "check_out_date": "2026-06-14",
            "nights": 4,
        },
    )
    session.add(hotel_seg)
    session.flush()
    logger.info("Sample trip + segments seeded.")
    return trip


# ---------------------------------------------------------------------------
# Step 4: Card transactions (aligned to sample trip)
# ---------------------------------------------------------------------------


def _seed_card_transactions(session: Session, org: Organization, employee: User) -> None:
    """Idempotent: guard by (user_id, merchant, txn_date, amount) composite.

    Templates are shared with topup_demo_card_feed (the live-safe top-up that
    restores an unmatched copy once smart-match has consumed one, C7-22).
    """
    for tpl in DEMO_CARD_TRANSACTIONS:
        existing = (
            session.query(CardTransaction)
            .filter(
                CardTransaction.user_id == employee.id,
                CardTransaction.merchant == tpl.merchant,
                CardTransaction.txn_date == tpl.txn_date,
                CardTransaction.amount == tpl.amount,
            )
            .first()
        )
        if existing is not None:
            continue
        tx = CardTransaction(
            organization_id=org.id,
            user_id=employee.id,
            expense_id=None,
            amount=tpl.amount,
            currency=tpl.currency,
            merchant=tpl.merchant,
            txn_date=tpl.txn_date,
            last_four=tpl.last_four,
            matched=False,
        )
        session.add(tx)
    session.flush()
    logger.info("Card transactions seeded.")


# ---------------------------------------------------------------------------
# Step 5: Calendar events (aligned to sample trip)
# ---------------------------------------------------------------------------


def _seed_calendar_events(session: Session, org: Organization, employee: User) -> None:
    """Idempotent: guard by (user_id, title, start_datetime) composite."""
    _EVENTS = [
        {
            "title": "Q2 Engineering Summit — Day 1",
            "location": "New York, NY",
            "start_datetime": datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
            "end_datetime": datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc),
        },
        {
            "title": "Q2 Engineering Summit — Day 2",
            "location": "New York, NY",
            "start_datetime": datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
            "end_datetime": datetime(2026, 6, 12, 17, 0, tzinfo=timezone.utc),
        },
    ]
    for ev_data in _EVENTS:
        existing = (
            session.query(CalendarEvent)
            .filter(
                CalendarEvent.user_id == employee.id,
                CalendarEvent.title == ev_data["title"],
                CalendarEvent.start_datetime == ev_data["start_datetime"],
            )
            .first()
        )
        if existing is not None:
            continue
        ev = CalendarEvent(
            organization_id=org.id,
            user_id=employee.id,
            title=ev_data["title"],
            location=ev_data["location"],
            start_datetime=ev_data["start_datetime"],
            end_datetime=ev_data["end_datetime"],
        )
        session.add(ev)
    session.flush()
    logger.info("Calendar events seeded.")


# ---------------------------------------------------------------------------
# Step 6: Receipts (inputs only — pipeline outputs are NULL)
# ---------------------------------------------------------------------------


def _seed_receipts(session: Session, org: Organization, employee: User) -> None:
    """Idempotent: guard by image_url (stable deterministic key)."""
    receipt_meta = [
        {"image_url": _RECEIPT_KEYS[0], "description": "Airfare receipt"},
        {"image_url": _RECEIPT_KEYS[1], "description": "Hotel receipt"},
        {"image_url": _RECEIPT_KEYS[2], "description": "Meal receipt"},
    ]
    for meta in receipt_meta:
        existing = session.query(Receipt).filter(Receipt.image_url == meta["image_url"]).first()
        if existing is not None:
            continue
        receipt = Receipt(
            organization_id=org.id,
            uploaded_by=employee.id,
            expense_id=None,
            image_url=meta["image_url"],
            ocr_status=OcrStatus.pending,
            processing_path=None,  # pipeline output — not fabricated
            extracted_fields=None,  # pipeline output — not fabricated
            field_confidence=None,  # pipeline output — not fabricated
            field_provenance=None,  # pipeline output — not fabricated
            grounding_context=None,  # pipeline output — not fabricated
        )
        session.add(receipt)
    session.flush()
    logger.info("Receipts seeded (%d pending rows).", len(receipt_meta))


# ---------------------------------------------------------------------------
# Step 8: Org chart extras (manager2 + employee2)
# ---------------------------------------------------------------------------


def _seed_org_chart_extras(session: Session, org: Organization) -> tuple[User, User]:
    """Idempotent: returns existing users if already seeded.

    Creates a second top-level manager (Brenda Taylor) and her direct report
    (Carlos Rivera), giving the analytics service a second reporting subtree
    so that finance(org) > manager(own+reports) > employee(own) are strictly
    distinct cardinalities.
    """
    from config.settings import get_settings  # noqa: PLC0415

    existing_manager2 = (
        session.query(User)
        .filter(User.organization_id == org.id, User.email == "manager2@atlas-demo.internal")
        .first()
    )
    if existing_manager2 is not None:
        logger.info("Org chart extras already seeded — skipping.")
        existing_employee2 = (
            session.query(User)
            .filter(User.organization_id == org.id, User.email == "employee2@atlas-demo.internal")
            .first()
        )
        assert existing_employee2 is not None, "manager2 exists but employee2 is missing"
        return existing_manager2, existing_employee2

    s = get_settings()
    hashed_pw = hash_password(s.SEED_DEFAULT_PASSWORD)

    manager2 = User(
        organization_id=org.id,
        name="Brenda Taylor",
        email="manager2@atlas-demo.internal",
        role=RoleKey.manager,
        department="Operations",
        manager_id=None,
        status=UserStatus.active,
        password_hash=hashed_pw,
    )
    session.add(manager2)
    session.flush()  # populate manager2.id for employee2 FK

    employee2 = User(
        organization_id=org.id,
        name="Carlos Rivera",
        email="employee2@atlas-demo.internal",
        role=RoleKey.employee,
        department="Operations",
        manager_id=manager2.id,
        status=UserStatus.active,
        password_hash=hashed_pw,
    )
    session.add(employee2)
    session.flush()
    logger.info("Org chart extras seeded: manager2 (Brenda Taylor), employee2 (Carlos Rivera).")
    return manager2, employee2


# ---------------------------------------------------------------------------
# Step 8b: Demo traveller grades (AIR-04, known limits)
# ---------------------------------------------------------------------------

# Deterministic demo grades (codes from config/constants/grades.py).  The demo
# manager is the SVP-level traveller, so AIR-04 (business class on flights > 6 h for
# SVPs and above) is demonstrable end to end; manager2 is a VP — one rung below — so
# the boundary is visible too.  Migration 0021 never guesses grades for real users.
DEMO_GRADES: dict[str, str] = {
    "manager@atlas-demo.internal": "SVP",
    "manager2@atlas-demo.internal": "VP",
    "finance@atlas-demo.internal": "DIRECTOR",
    "employee2@atlas-demo.internal": "SENIOR_IC",
    "employee@atlas-demo.internal": "IC",
}


def _seed_demo_grades(
    session: Session, org: Organization, grades: dict[str, str] | None = None
) -> int:
    """Set the demo users' grades — ONLY where none is recorded yet (idempotent).

    A grade a finance admin has already set is never
    overwritten; only demo-org accounts listed in ``DEMO_GRADES`` are touched.
    Returns the number of users updated.
    """
    updated = 0
    for email, code in (DEMO_GRADES if grades is None else grades).items():
        user = (
            session.query(User).filter(User.organization_id == org.id, User.email == email).first()
        )
        if user is None or user.grade is not None:
            continue
        user.grade = code
        updated += 1
    if updated:
        session.flush()
    logger.info("Demo grades seeded: %d user(s).", updated)
    return updated


# ---------------------------------------------------------------------------
# Step 9: Expense reports + approvals
# ---------------------------------------------------------------------------


def _seed_expense_reports_and_approvals(  # noqa: PLR0914
    session: Session,
    org: Organization,
    employee: User,
    manager: User,
    employee2: User,
    manager2: User,  # noqa: ARG001 — reserved for future reports owned by manager2
) -> None:
    """Idempotent: skips entirely if any ExpenseReport rows exist for this org.

    Creates 5 expense reports across two employees with varied statuses, expense
    entries, and linked approvals/steps so the analytics service returns non-empty
    payloads at all role scopes.
    """
    existing_count = (
        session.query(ExpenseReport).filter(ExpenseReport.organization_id == org.id).count()
    )
    if existing_count > 0:
        logger.info("Expense reports already seeded (%d rows) — skipping.", existing_count)
        return

    utc = timezone.utc

    # ------------------------------------------------------------------
    # Report 1: employee — submitted + approved (Q2 Summit Expenses).  The
    # report carries the decision outcome (C7R / P12: an approved approval
    # moves its report to approved).
    # ------------------------------------------------------------------
    report1 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee.id,
        title="Q2 Summit Expenses",
        status=ExpenseReportStatus.approved,
        currency="USD",
        total_amount=Decimal("1351.00"),
        submitted_at=datetime(2026, 5, 15, tzinfo=utc),
        pre_submit_flags=None,
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(report1)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report1.id,
            category=ExpenseCategory.airfare,
            amount=Decimal("320.00"),
            currency="USD",
            expense_date=date(2026, 5, 15),
            description="Airfare SFO→NYC",
            status=ExpenseStatus.approved,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report1.id,
            category=ExpenseCategory.lodging,
            amount=Decimal("956.00"),
            currency="USD",
            expense_date=date(2026, 5, 15),
            description="Hotel — 4 nights",
            status=ExpenseStatus.approved,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report1.id,
            category=ExpenseCategory.meals,
            amount=Decimal("75.00"),
            currency="USD",
            expense_date=date(2026, 5, 16),
            description="Team dinner",
            status=ExpenseStatus.approved,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    approval1 = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report1.id,
        submitter_id=employee.id,
        current_status=ApprovalStatus.approved,
        is_auto_approved=False,
        risk_summary=None,
    )
    session.add(approval1)
    session.flush()

    session.add(
        ApprovalStep(
            approval_id=approval1.id,
            approver_id=manager.id,
            step_order=1,
            decision=ApprovalDecision.approve,
            reason=None,
            decided_at=datetime(2026, 5, 16, tzinfo=utc),
        )
    )

    # ------------------------------------------------------------------
    # Report 2: employee — draft (June Miscellaneous)
    # ------------------------------------------------------------------
    report2 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee.id,
        title="June Miscellaneous",
        status=ExpenseReportStatus.draft,
        currency="USD",
        total_amount=Decimal("73.00"),
        submitted_at=None,
        pre_submit_flags=None,
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(report2)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report2.id,
            category=ExpenseCategory.meals,
            amount=Decimal("45.00"),
            currency="USD",
            expense_date=date(2026, 6, 5),
            description="Working lunch",
            status=ExpenseStatus.draft,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report2.id,
            category=ExpenseCategory.ground_transport,
            amount=Decimal("28.00"),
            currency="USD",
            expense_date=date(2026, 6, 6),
            description="Rideshare to client site",
            status=ExpenseStatus.draft,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )

    # ------------------------------------------------------------------
    # Report 3: employee2 — submitted, pending approval (Client Visit)
    # ------------------------------------------------------------------
    report3 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee2.id,
        title="Client Visit Expenses",
        status=ExpenseReportStatus.submitted,
        currency="USD",
        total_amount=Decimal("400.00"),
        submitted_at=datetime(2026, 6, 1, tzinfo=utc),
        pre_submit_flags=None,
        created_by=employee2.id,
        updated_by=employee2.id,
    )
    session.add(report3)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report3.id,
            category=ExpenseCategory.airfare,
            amount=Decimal("280.00"),
            currency="USD",
            expense_date=date(2026, 6, 1),
            description="Airfare to client site",
            status=ExpenseStatus.draft,
            created_by=employee2.id,
            updated_by=employee2.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report3.id,
            category=ExpenseCategory.meals,
            amount=Decimal("120.00"),
            currency="USD",
            expense_date=date(2026, 6, 2),
            description="Client dinner",
            status=ExpenseStatus.draft,
            created_by=employee2.id,
            updated_by=employee2.id,
        )
    )
    approval3 = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report3.id,
        submitter_id=employee2.id,
        current_status=ApprovalStatus.pending,
        is_auto_approved=False,
        risk_summary=None,
    )
    session.add(approval3)

    # ------------------------------------------------------------------
    # Report 4: employee — submitted + rejected (Conference Expenses)
    # ------------------------------------------------------------------
    report4 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee.id,
        title="Conference Expenses (Rejected)",
        status=ExpenseReportStatus.rejected,
        currency="USD",
        total_amount=Decimal("545.00"),
        submitted_at=datetime(2026, 4, 10, tzinfo=utc),
        pre_submit_flags={
            "warnings": [
                {
                    "severity": "high",
                    "message": "Lodging rate exceeds policy limit",
                    "clause_ref": "§4.2",
                }
            ]
        },
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(report4)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report4.id,
            category=ExpenseCategory.lodging,
            amount=Decimal("450.00"),
            currency="USD",
            expense_date=date(2026, 4, 10),
            description="Hotel — over policy limit",
            status=ExpenseStatus.flagged,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report4.id,
            category=ExpenseCategory.meals,
            amount=Decimal("95.00"),
            currency="USD",
            expense_date=date(2026, 4, 11),
            description="Conference meals",
            status=ExpenseStatus.draft,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    approval4 = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report4.id,
        submitter_id=employee.id,
        current_status=ApprovalStatus.rejected,
        is_auto_approved=False,
        risk_summary=None,
    )
    session.add(approval4)
    session.flush()

    session.add(
        ApprovalStep(
            approval_id=approval4.id,
            approver_id=manager.id,
            step_order=1,
            decision=ApprovalDecision.reject,
            reason="Lodging exceeds policy",
            decided_at=datetime(2026, 4, 11, tzinfo=utc),
        )
    )

    # ------------------------------------------------------------------
    # Report 5: employee2 — auto-approved (Office Supplies)
    # ------------------------------------------------------------------
    report5 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee2.id,
        title="Office Supplies",
        status=ExpenseReportStatus.approved,
        currency="USD",
        total_amount=Decimal("85.00"),
        submitted_at=datetime(2026, 5, 20, tzinfo=utc),
        pre_submit_flags=None,
        created_by=employee2.id,
        updated_by=employee2.id,
    )
    session.add(report5)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report5.id,
            category=ExpenseCategory.other,
            amount=Decimal("85.00"),
            currency="USD",
            expense_date=date(2026, 5, 20),
            description="Desk supplies",
            status=ExpenseStatus.approved,
            created_by=employee2.id,
            updated_by=employee2.id,
        )
    )
    approval5 = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report5.id,
        submitter_id=employee2.id,
        current_status=ApprovalStatus.auto_approved,
        is_auto_approved=True,
        # Real Approval-Agent build_risk_summary() shape (auto_approve route) —
        # NOT the legacy {"auto_approved": ...} placeholder, so the dashboard's
        # one risk_summary mapper handles seed + real-agent payloads identically.
        risk_summary={
            "route": "auto_approve",
            "subject_type": "expense_report",
            "subject_in_policy": True,
            "blocking_flag_count": 0,
            "requires_approver": False,
            "subject_total_usd": 85.00,
            "flags": [],
        },
    )
    session.add(approval5)
    session.flush()

    # System auto-approve step — its `reason` is the rationale shown in the
    # auto-approved lane (mirrors approval_agent persist_approval's
    # _build_auto_approve_rationale; approver_id NULL = system action).
    session.add(
        ApprovalStep(
            approval_id=approval5.id,
            approver_id=None,
            step_order=1,
            decision=ApprovalDecision.auto_approve,
            reason=(
                "Auto-approved: subject is fully in policy, zero blocking flags, "
                "no approver required by policy, total 85.00 USD ≤ threshold 500.00 USD."
            ),
            decided_at=datetime(2026, 5, 20, tzinfo=utc),
        )
    )

    # ------------------------------------------------------------------
    # Report 6: employee (Alex) — submitted, PENDING for Morgan (P5-T8)
    # Policy-flagged so the decision panel renders real exceptions + citations.
    # ------------------------------------------------------------------
    report6 = ExpenseReport(
        organization_id=org.id,
        owner_id=employee.id,
        title="Q3 Conference & Travel",
        status=ExpenseReportStatus.submitted,
        currency="USD",
        total_amount=Decimal("1234.50"),
        submitted_at=datetime(2026, 6, 12, tzinfo=utc),
        pre_submit_flags={
            "flags": _q3_flags(),
            "evaluated_entry_count": 3,
            "blocking_count": 1,
            "advisory_count": 1,
        },
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(report6)
    session.flush()

    session.add(
        Expense(
            organization_id=org.id,
            report_id=report6.id,
            category=ExpenseCategory.lodging,
            amount=Decimal("867.00"),
            currency="USD",
            expense_date=date(2026, 6, 10),
            vendor=Q3_LODGING_VENDOR,
            description=Q3_LODGING_DESCRIPTION,
            status=ExpenseStatus.flagged,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report6.id,
            category=ExpenseCategory.meals,
            amount=Decimal("205.50"),
            currency="USD",
            expense_date=date(2026, 6, 11),
            description=Q3_MEALS_DESCRIPTION,
            status=ExpenseStatus.draft,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    session.add(
        Expense(
            organization_id=org.id,
            report_id=report6.id,
            category=ExpenseCategory.ground_transport,
            amount=Decimal("162.00"),
            currency="USD",
            expense_date=date(2026, 6, 12),
            description="Rideshare & airport parking",
            status=ExpenseStatus.draft,
            created_by=employee.id,
            updated_by=employee.id,
        )
    )
    approval6 = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report6.id,
        submitter_id=employee.id,
        current_status=ApprovalStatus.pending,
        is_auto_approved=False,
        # Real Approval-Agent build_risk_summary() shape (escalate, expense_report).
        risk_summary={
            "route": "escalate",
            "subject_type": "expense_report",
            "subject_in_policy": False,
            "blocking_flag_count": 2,
            "requires_approver": False,
            "subject_total_usd": 1234.50,
            "routed_to_approver_id": str(manager.id),
            "flags": _q3_flags(),
        },
    )
    session.add(approval6)

    # ------------------------------------------------------------------
    # Trip approval: employee (Alex) — trip-subject PENDING for Morgan (P5-T8).
    # Exercises subject polymorphism (trip vs expense_report) in the panel.
    # ------------------------------------------------------------------
    trip_pending = Trip(
        organization_id=org.id,
        traveler_id=employee.id,
        origin="LAX",
        primary_destination="ORD",
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 17),
        purpose="Client presentation — Chicago",
        estimated_cost=Decimal("1410.00"),
        budget_amount=Decimal("1800.00"),
        currency="USD",
        status=TripStatus.pending_approval,
        recommendation_context=None,
        created_by=employee.id,
        updated_by=employee.id,
    )
    session.add(trip_pending)
    session.flush()

    trip_flight = TripSegment(
        trip_id=trip_pending.id,
        segment_type=SegmentType.flight,
        supplier="American Airlines",
        start_datetime=datetime(2026, 7, 15, 10, 0, tzinfo=utc),
        end_datetime=datetime(2026, 7, 15, 16, 30, tzinfo=utc),
        location="Chicago, IL",
        amount=Decimal("520.00"),
        currency="USD",
        in_policy=True,
        policy_notes=None,
        details={
            "flight_number": "AA215",
            "cabin_class": "economy",
            "origin_airport": "LAX",
            "destination_airport": "ORD",
            "is_direct": True,
        },
    )
    session.add(trip_flight)

    trip_hotel = TripSegment(
        trip_id=trip_pending.id,
        segment_type=SegmentType.hotel,
        supplier="Hyatt Regency",
        start_datetime=datetime(2026, 7, 15, 16, 0, tzinfo=utc),
        end_datetime=datetime(2026, 7, 17, 11, 0, tzinfo=utc),
        location="Chicago, IL",
        amount=Decimal("890.00"),  # 2 nights × $445 — over cap
        currency="USD",
        in_policy=False,
        policy_notes=CHICAGO_HOTEL_REASON,
        details={
            "hotel_name": "Hyatt Regency Chicago",
            "room_type": "Standard",
            "nightly_rate_usd": 445.00,
            "nights": 2,
        },
    )
    session.add(trip_hotel)
    session.flush()

    approval_trip = Approval(
        organization_id=org.id,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip_pending.id,
        submitter_id=employee.id,
        current_status=ApprovalStatus.pending,
        is_auto_approved=False,
        # Real Approval-Agent build_risk_summary() shape (escalate, trip).
        risk_summary={
            "route": "escalate",
            "subject_type": "trip",
            "subject_in_policy": False,
            "blocking_flag_count": 0,
            "requires_approver": False,
            "subject_total_usd": 1410.00,
            "routed_to_approver_id": str(manager.id),
            "flags": [
                {
                    "segment_id": str(trip_flight.id),
                    "segment_type": "flight",
                    "in_policy": True,
                    "policy_notes": None,
                },
                {
                    "segment_id": str(trip_hotel.id),
                    "segment_type": "hotel",
                    "in_policy": False,
                    "policy_notes": CHICAGO_HOTEL_REASON,
                },
            ],
        },
    )
    session.add(approval_trip)

    session.flush()
    logger.info(
        "Expense reports + approvals seeded: 6 reports, 6 approvals "
        "(incl. 1 trip-subject), 3 decision steps."
    )


# ---------------------------------------------------------------------------
# Demo policy-flag texts (Issue 6) — the EXACT reasons PolicyEvalTool produces
# for the seeded lines under the canonical TEP-FY26 figures (figures.py), so the
# seeded approvals quote the same caps as the engine and policy Q&A.  Asserted
# against a fresh evaluation by backend/tests/test_issue6_seed_coherence_db.py.
# ---------------------------------------------------------------------------
_LOD = F.LODGING_CAP
_PD = F.PER_DIEM
Q3_LODGING_VENDOR = "New York Marriott Marquis"
Q3_LODGING_DESCRIPTION = "Hotel — 3 nights @ 289.00/night"
Q3_MEALS_DESCRIPTION = "Conference meals (3 days)"
Q3_HOTEL_REASON = (
    "Hotel amount 867.00 USD over the 3 nights stated on the entry (not verified) is "
    f"289.00 USD per night — within the Tier 1 New York cap of ${_LOD['tier1']}/night "
    "if the city is confirmed. Hotel city could not be verified from a receipt or "
    f"approved itinerary, so the lowest cap (Tier 3, {_LOD['tier3']:.2f} USD per night) "
    "was applied. The number of nights could not be verified from a receipt or approved "
    "itinerary. Your approver must confirm the stay length and the hotel city. "
    "Requires manager pre-approval."
)
Q3_MEALS_REASON = (
    "Meal amount 205.50 USD over the 3 days stated on the entry (not verified) is "
    f"68.50 USD per day, which exceeds the Tier 3 per diem {_PD['tier3']:.2f} USD per day. "
    "Meal city could not be verified from a receipt or approved itinerary, so the lowest "
    f"per diem (Tier 3, {_PD['tier3']:.2f} USD per day) was applied. The number of days "
    "could not be verified from a receipt or approved itinerary."
)
CHICAGO_HOTEL_REASON = (
    f"Nightly rate 445.00 USD (2 nights) exceeds the Tier 2 cap {_LOD['tier2']:.2f} USD "
    "per night (city: Chicago, IL). Requires manager pre-approval."
)


def _q3_flags() -> list[dict[str, Any]]:
    """The Q3 report's HOTEL-CAP / MEALS-PD flags (engine shape, rule ids omitted)."""
    return [
        {
            "entry_ref": Q3_LODGING_VENDOR,
            "flag_type": "policy_violation",
            "rule_id": None,
            "rule_type": "amount_cap",
            "source_clause_ref": "HOTEL-CAP",
            "reason": Q3_HOTEL_REASON,
            "requires_approver": "manager",
            "observed": {
                "amount_usd": 867.0,
                "cap": float(_LOD["tier3"]),
                "currency": "USD",
                "tier": "tier3",
                "city": None,
                "tier_label": "Tier 3",
                "claimed_nights": 3,
                "claimed_nightly_usd": 289.0,
                "claimed_city": "New York",
                "claimed_tier": "tier1",
                "claimed_tier_label": "Tier 1",
                "claimed_cap": float(_LOD["tier1"]),
                "within_cap_if_verified": True,
                "unverified": ["nights", "city"],
            },
            "severity": "advisory",
            "justifiable": True,
            "justification": None,
            "pending_verification": "nights_and_city",
        },
        {
            "entry_ref": Q3_MEALS_DESCRIPTION,
            "flag_type": "policy_violation",
            "rule_id": None,
            "rule_type": "per_diem",
            "source_clause_ref": "MEALS-PD",
            "reason": Q3_MEALS_REASON,
            "requires_approver": None,
            "observed": {
                "amount_usd": 205.5,
                "rate": float(_PD["tier3"]),
                "tier": "tier3",
                "city": None,
                "tier_label": "Tier 3",
                "claimed_days": 3,
                "claimed_daily_usd": 68.5,
                "within_cap_if_verified": False,
            },
            "severity": "blocking",
            "justifiable": True,
            "justification": None,
            "pending_verification": None,
        },
    ]


# Subject status a DECIDED approval implies (C7R / P12).  Pending approvals
# imply nothing (the subject stays in its submitted state).
_REPORT_STATUS_FOR_APPROVAL: dict[ApprovalStatus, ExpenseReportStatus] = {
    ApprovalStatus.approved: ExpenseReportStatus.approved,
    ApprovalStatus.auto_approved: ExpenseReportStatus.approved,
    ApprovalStatus.rejected: ExpenseReportStatus.rejected,
    ApprovalStatus.changes_requested: ExpenseReportStatus.draft,
}
_TRIP_STATUS_FOR_APPROVAL: dict[ApprovalStatus, TripStatus] = {
    ApprovalStatus.approved: TripStatus.approved,
    ApprovalStatus.auto_approved: TripStatus.approved,
    ApprovalStatus.rejected: TripStatus.rejected,
    ApprovalStatus.changes_requested: TripStatus.draft,
}


def _reconcile_decided_subjects(session: Session, org: Organization) -> int:
    """Idempotent: move subjects still 'submitted' to their decided approval's outcome.

    The reports step skips once reports exist, so a database seeded before the
    C7R / P12 fix keeps e.g. "Q2 Summit Expenses" ``submitted`` under an
    ``approved`` approval — which drops it from the finance KPIs.  Only a subject
    still in its submitted state (report ``submitted`` / trip
    ``pending_approval``) is touched; anything the workflow has since moved on is
    left alone.  Returns the number of subjects repaired.
    """
    repaired = 0
    approvals = (
        session.query(Approval)
        .filter(
            Approval.organization_id == org.id,
            Approval.current_status.in_(list(_REPORT_STATUS_FOR_APPROVAL)),
        )
        .all()
    )
    for approval in approvals:
        subject: ExpenseReport | Trip | None
        if approval.subject_type == ApprovalSubjectType.expense_report:
            subject = session.get(ExpenseReport, approval.subject_id)
            if subject is None or subject.status != ExpenseReportStatus.submitted:
                continue
            subject.status = _REPORT_STATUS_FOR_APPROVAL[approval.current_status]
            if subject.status == ExpenseReportStatus.draft:
                subject.submitted_at = None
        else:
            subject = session.get(Trip, approval.subject_id)
            if subject is None or subject.status != TripStatus.pending_approval:
                continue
            subject.status = _TRIP_STATUS_FOR_APPROVAL[approval.current_status]
        subject.version = subject.version + 1  # invalidate stale If-Match copies
        repaired += 1
    session.flush()
    if repaired:
        logger.info("Reconciled %d subject(s) with their decided approval.", repaired)
    return repaired


# ---------------------------------------------------------------------------
# Step 7: MinIO upload (lazy — skips gracefully if unavailable)
# ---------------------------------------------------------------------------


def _upload_receipt_images() -> None:
    """Upload sample PNG images to the dev MinIO bucket.

    Reads STORAGE_* from os.environ directly (Settings ignores these fields).
    Wrapped in try/except — skips gracefully if boto3 or MinIO is unavailable.
    Idempotent: put_object overwrites the same keys on re-run.
    """
    endpoint = os.environ.get("STORAGE_ENDPOINT_URL", "http://localhost:9000")
    bucket = os.environ.get("STORAGE_BUCKET", "atlas-receipts-dev")
    access_key = os.environ.get("STORAGE_ACCESS_KEY_ID", "minioadmin")
    secret_key = os.environ.get("STORAGE_SECRET_ACCESS_KEY", "")

    if not secret_key:
        logger.info("STORAGE_SECRET_ACCESS_KEY not set — skipping MinIO receipt image upload.")
        return

    try:
        import boto3  # lazy import — dev-only dep  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
        from botocore.exceptions import ClientError  # noqa: PLC0415

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.environ.get("STORAGE_REGION", "us-east-1"),
            config=Config(s3={"addressing_style": "path"}),
        )

        # Ensure bucket exists
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                s3.create_bucket(Bucket=bucket)
                logger.info("Created MinIO bucket: %s", bucket)
            else:
                raise

        for key in _RECEIPT_KEYS:
            s3.put_object(Bucket=bucket, Key=key, Body=_SAMPLE_PNG_BYTES)
            logger.info("Uploaded receipt image: s3://%s/%s", bucket, key)

        logger.info("MinIO receipt image upload complete.")

    except ImportError:
        logger.info("boto3 not available — skipping MinIO receipt image upload.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("MinIO upload skipped (error: %s).", exc)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def seed(session_factory=None) -> None:  # type: ignore[assignment]
    """Run the full Atlas seed in dependency order.  Idempotent.

    Pass a session_factory to override (useful in tests).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if session_factory is None:
        session_factory = get_sessionmaker()

    # Step 1: Identity (RBAC catalog + org + users)
    seed_identity(session_factory=session_factory)

    with session_factory() as session:
        # Resolve org + users for FK references
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()

        # Scoped to the demo org: email is unique per organization (migration 0022).
        def _demo_user(email: str) -> User:
            return (
                session.query(User)
                .filter(User.organization_id == org.id, User.email == email)
                .one()
            )

        employee = _demo_user("employee@atlas-demo.internal")
        manager = _demo_user("manager@atlas-demo.internal")
        finance_admin = _demo_user("finance@atlas-demo.internal")

        # Step 1b: Resync passwords (idempotent; fixes stale hashes from prior seed runs)
        _resync_user_passwords(session)
        session.commit()

        # Step 2: Policy + rules + embeddings (legacy single-policy stub)
        _seed_policy(session, org, finance_admin)
        session.commit()

        # Step 2a: Multi-document Helios T&E corpus (R0-T2) — additive document rows.
        _seed_corpus(session, org, finance_admin)
        session.commit()

        # Step 2a': Chunk + embed the corpus (R0-T3/T4) and RETIRE the legacy stub's
        # embeddings so RAG retrieval serves ONLY the Helios corpus.
        _embed_corpus(session, org)
        _retire_stub_embeddings(session, org)
        session.commit()

        # Step 2b: One DRAFT rule (exercises navigator draft dots + activation)
        _seed_draft_rule(session, org, finance_admin)
        session.commit()

        # Step 3: Sample trip + segments
        _seed_sample_trip(session, org, employee)
        session.commit()

        # Step 4: Card transactions
        _seed_card_transactions(session, org, employee)

        # Step 5: Calendar events
        _seed_calendar_events(session, org, employee)

        # Step 6: Receipts
        _seed_receipts(session, org, employee)

        session.commit()

        # Step 8: Org chart extras (manager2 + employee2)
        manager2, employee2 = _seed_org_chart_extras(session, org)
        session.commit()

        # Step 8b: Demo traveller grades (AIR-04; only where none is recorded)
        _seed_demo_grades(session, org)
        session.commit()

        # Step 9: Expense reports + approvals
        _seed_expense_reports_and_approvals(session, org, employee, manager, employee2, manager2)
        session.commit()

        # Step 9b: Repair subjects left 'submitted' under a decided approval by an
        # older seed (e.g. "Q2 Summit Expenses"); a no-op on a fresh seed.
        _reconcile_decided_subjects(session, org)
        session.commit()

    # Step 7: MinIO receipt images (outside DB transaction; self-skipping)
    _upload_receipt_images()

    print("\nAtlas seed complete.")
    print("  Organization : Atlas Demo Corp")
    print("  Users        : employee@, manager@, finance@, manager2@, employee2@")
    print("                 (all @atlas-demo.internal)")
    print("  Policy       : Atlas Global T&E Policy v1 (stub rules kept, RAG retired)")
    rule_count = len(RULES) + len(ENTRY_RULES) + len(PROCESS_RULES)
    print(f"  Policy rules : {rule_count} stub rules, 0 stub embeddings")
    print(
        f"  RAG corpus   : {len(ALL_DOCUMENTS)} Helios documents, "
        f"{sum(d.unit_count for d in ALL_DOCUMENTS)} embeddings"
    )
    print("  Sample trip  : SFO → JFK (2026-06-10 to 2026-06-14, completed)")
    print("  Pending trip : LAX → ORD (2026-07-15 to 2026-07-17, pending_approval)")
    print("  Receipts     : 3 pending rows (MinIO images attempted separately)")
    print("  Exp. reports : 6 reports (submitted/draft/rejected/approved/auto_approved)")
    print("  Approvals    : 6 approvals (approved, pending×2, rejected, auto_approved, trip)")


if __name__ == "__main__":
    seed()
