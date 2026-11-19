"""P3-T5 Validate gate: seed data tests.

Self-skips when Postgres is unreachable or the full migration chain has not been run
(pgvector / policy_embeddings table required).

Tests:
  (a) Seed runs clean on a migrated DB; all expected entities are present.
  (b) Re-running seed is idempotent — row counts are identical, no duplicates.
  (c) Embeddings integrity: count == the corpus unit total (219; the stub's 8 are
      retired in R0-T4), dimension == EMBEDDING_DIMENSION, ANN similarity query
      returns ≥1 clause_ref (proves HNSW + seeded vectors work).
  (d) Rules ↔ clauses consistency: 7 active rules, each source_clause_ref resolves
      to a real stub CLAUSES clause_ref (the authoring pair, not the live index).
  (e) Auth: seeded employee login returns tokens; GET /me returns that user;
      bad password → standard 401 envelope (no user enumeration).
  (f) Mock inventory: search_flights + search_hotels return options; at least one
      out-of-policy-priced hotel exists.
  (g) Phase-2 CRUD checkpoint: create a trip as the seeded employee → 201 draft.
  (h) Receipts: 3 pending rows, all extraction JSONB columns are NULL.
  (i) MinIO: self-skips if boto3 or MinIO is unreachable; otherwise verifies each
      receipt image_url key is fetchable from the bucket.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.main_entry.app import app
from config.constants.embeddings import EMBEDDING_DIMENSION
from config.settings import get_settings
from database.seeds._local_embedder import embed
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.policy_content import CLAUSES, ENTRY_RULES, PROCESS_RULES, RULES
from services.mock_inventory import search_flights, search_hotels

pytestmark = pytest.mark.db

_settings = get_settings()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield engine if Postgres is reachable AND full migration chain has run.

    Skips the entire module if either condition is not met — the seed tests
    require the real migrated schema (pgvector extension + policy_embeddings).
    """
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping P3-T5 seed tests")

    # Verify the migration chain has been run (check for policy_embeddings)
    insp = sa.inspect(engine)
    if "policy_embeddings" not in insp.get_table_names():
        pytest.skip("policy_embeddings table not found — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    """Run the full seed (idempotent) and return resolved IDs."""
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()  # idempotent — safe to call on already-seeded DB

    factory = get_sessionmaker()
    from database.models.identity import Organization, User  # noqa: PLC0415

    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        employee = session.query(User).filter(User.email == "employee@atlas-demo.internal").one()
        manager = session.query(User).filter(User.email == "manager@atlas-demo.internal").one()

    return {
        "org_id": str(org.id),
        "employee_id": str(employee.id),
        "manager_id": str(manager.id),
        "employee_email": "employee@atlas-demo.internal",
        "seed_password": _settings.SEED_DEFAULT_PASSWORD,
    }


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# (a/b) Seed clean + idempotency
# ---------------------------------------------------------------------------


def test_seed_runs_clean_and_is_idempotent(db_engine: Engine, seeded: dict) -> None:
    """Seed has already run via the seeded fixture; run again and verify counts are stable."""
    from database.models.calendar_event import CalendarEvent  # noqa: PLC0415
    from database.models.card_transaction import CardTransaction  # noqa: PLC0415
    from database.models.policy import Policy  # noqa: PLC0415
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415
    from database.models.policy_rule import PolicyRule  # noqa: PLC0415
    from database.models.receipt import Receipt  # noqa: PLC0415
    from database.models.trip import Trip  # noqa: PLC0415
    from database.models.trip_segment import TripSegment  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    factory = get_sessionmaker()

    def _counts(session: Any) -> dict[str, int]:
        return {
            "policy": session.query(Policy).count(),
            "rules": session.query(PolicyRule).count(),
            "embeddings": session.query(PolicyEmbedding).count(),
            "trips": session.query(Trip).count(),
            "segments": session.query(TripSegment).count(),
            "card_txns": session.query(CardTransaction).count(),
            "calendar": session.query(CalendarEvent).count(),
            "receipts": session.query(Receipt).count(),
        }

    with factory() as session:
        before = _counts(session)

    # Second seed run (the fixture already ran the first)
    seed()

    with factory() as session:
        after = _counts(session)

    assert before == after, f"Seed is not idempotent: {before} → {after}"
    # Sanity: expected minimum rows are present
    assert after["policy"] >= 1
    assert after["rules"] >= 7
    assert after["embeddings"] >= 8
    assert after["trips"] >= 1
    assert after["receipts"] >= 3


# ---------------------------------------------------------------------------
# (c) Embeddings integrity + HNSW ANN similarity query
# ---------------------------------------------------------------------------


def test_policy_embeddings_integrity(db_engine: Engine, seeded: dict) -> None:
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        embeddings = session.query(PolicyEmbedding).all()

    # R0-T4: the legacy stub's 8 embeddings are RETIRED; retrieval is served by the
    # four-document Helios corpus (one embedding per unit), so the live count is the
    # total corpus units (100 + 59 + 35 + 25 = 219), not len(CLAUSES).
    expected = sum(d.unit_count for d in ALL_DOCUMENTS)
    assert (
        len(embeddings) == expected
    ), f"Expected {expected} corpus embeddings, got {len(embeddings)}"
    for emb in embeddings:
        vec: list[float] = list(emb.embedding)  # type: ignore[arg-type, call-overload]
        # pgvector returns as a list-like; verify dimension
        assert len(vec) == EMBEDDING_DIMENSION, (
            f"Embedding dimension mismatch for clause_ref={emb.clause_ref}: "
            f"expected {EMBEDDING_DIMENSION}, got {len(vec)}"
        )
        assert emb.clause_ref is not None


def test_ann_similarity_query_returns_clause(db_engine: Engine, seeded: dict) -> None:
    """Embed a query and run a cosine ANN query — proves HNSW path works end-to-end."""
    query_vec = embed("hotel nightly cap")

    with get_engine().connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT clause_ref, chunk_text "
                "FROM policy_embeddings "
                "ORDER BY embedding <=> CAST(:q AS vector) "
                "LIMIT 3"
            ),
            {"q": str(query_vec)},
        )
        rows = result.fetchall()

    assert len(rows) >= 1, "ANN similarity query returned no rows"
    clause_refs = [r[0] for r in rows]
    assert any(r is not None for r in clause_refs), "No clause_ref in top-3 ANN results"


# ---------------------------------------------------------------------------
# (d) Rules ↔ clauses consistency
# ---------------------------------------------------------------------------


def test_policy_rules_consistency(db_engine: Engine, seeded: dict) -> None:
    from database.models.policy_rule import PolicyRule, PolicyRuleStatus  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        # Scope to ACTIVE rules: the seed also adds one DRAFT rule (navigator demo),
        # so an unfiltered count is 8, not len(RULES)==7.
        rules = session.query(PolicyRule).filter(PolicyRule.status == PolicyRuleStatus.active).all()

    # The stub's RAG embeddings are RETIRED in R0-T4 (the Helios corpus serves
    # retrieval), so a stub rule's source_clause_ref no longer resolves against the
    # LIVE policy_embeddings.  Rule provenance is a property of the policy_content
    # authoring pair (RULES <-> CLAUSES) — validate against the static CLAUSES.
    stub_clause_refs = {c["clause_ref"] for c in CLAUSES}
    # Product gap 2 (intended change): the entry-level rules (GRND-01 / 03 / 05) are
    # seeded with the stub and cite the canonical TEP-FY26 corpus clause directly.
    policy_doc = next(d for d in ALL_DOCUMENTS if d.document_type == "policy")
    corpus_clause_refs = {u.id for u in policy_doc.units}
    # Product gap 2 stage 2 (intended change): so are the process rules (EXP-01, PTA-02,
    # PTA-05, AIR-06, CARD-03).
    entry_refs = {r["clause_ref"] for r in (*ENTRY_RULES, *PROCESS_RULES)}
    assert entry_refs <= corpus_clause_refs
    expected = len(RULES) + len(ENTRY_RULES) + len(PROCESS_RULES)
    assert len(rules) == expected, f"Expected {expected} active rules, got {len(rules)}"
    # Product gap 2 (policy2): the spend-cap / gift / mileage rules (appended to RULES)
    # cite TEP-FY26 clause ids too — each must be a real one.
    from database.seeds.spend_rules_content import SPEND_RULES  # noqa: PLC0415

    spend_refs = {r["clause_ref"] for r in SPEND_RULES}
    assert spend_refs <= corpus_clause_refs
    for rule in rules:
        assert rule.source_clause_ref in stub_clause_refs | entry_refs | spend_refs, (
            f"Rule source_clause_ref '{rule.source_clause_ref}' not in stub CLAUSES / "
            "ENTRY_RULES / PROCESS_RULES / SPEND_RULES"
        )
        assert rule.status.value == "active"


# ---------------------------------------------------------------------------
# (e) Auth: seeded employee login
# ---------------------------------------------------------------------------


def test_seeded_login_returns_token(seeded: dict, client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": seeded["employee_email"], "password": seeded["seed_password"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


def test_seeded_login_me_returns_user(seeded: dict, client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": seeded["employee_email"], "password": seeded["seed_password"]},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == seeded["employee_email"]
    assert "password_hash" not in body


def test_seeded_login_bad_password_returns_401(seeded: dict, client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": seeded["employee_email"], "password": "wrong-password"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# (f) Mock inventory query helper
# ---------------------------------------------------------------------------


def test_mock_inventory_flights_returns_options(seeded: dict) -> None:
    flights = search_flights("SFO", "JFK", "2026-06-10")
    assert len(flights) >= 1, "search_flights returned no results for SFO→JFK 2026-06-10"


def test_mock_inventory_hotels_returns_options(seeded: dict) -> None:
    hotels = search_hotels("New York", "2026-06-10", "2026-06-14")
    assert len(hotels) >= 1, "search_hotels returned no results for NYC"


def test_mock_inventory_has_out_of_policy_hotel(seeded: dict) -> None:
    hotels = search_hotels("New York", "2026-06-10", "2026-06-14")
    # Tier-1 NYC cap is $250/night; at least one hotel should exceed it
    out_of_policy = [h for h in hotels if h.nightly_rate_usd > 250]
    assert (
        len(out_of_policy) >= 1
    ), "No out-of-policy hotel found in NYC dataset (expected at least one > $250/night)"


# ---------------------------------------------------------------------------
# (g) Phase-2 CRUD checkpoint: create a trip via the API as seeded employee
# ---------------------------------------------------------------------------


def test_phase2_crud_checkpoint_create_trip(
    db_engine: Engine, seeded: dict, client: TestClient
) -> None:
    # Login as the seeded employee
    r = client.post(
        "/api/v1/auth/login",
        json={"email": seeded["employee_email"], "password": seeded["seed_password"]},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]

    # Create a trip via the API (this is a new trip, independent of the seeded one)
    payload = {
        "origin": "SFO",
        "primary_destination": "BOS",
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "currency": "USD",
        "purpose": "Checkpoint CRUD test trip",
    }
    r = client.post(
        "/api/v1/trips",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["status"] == "draft"
    assert body["origin"] == "SFO"
    assert body["primary_destination"] == "BOS"


# ---------------------------------------------------------------------------
# (h) Receipts: pending rows with NULL extraction fields
# ---------------------------------------------------------------------------


def test_receipts_are_pending_with_null_extraction(db_engine: Engine, seeded: dict) -> None:
    from database.models.receipt import OcrStatus, Receipt  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        receipts = session.query(Receipt).filter(Receipt.image_url.like("receipts/seed/%")).all()

    assert len(receipts) >= 3, f"Expected ≥3 seeded receipts, got {len(receipts)}"
    for receipt in receipts:
        assert receipt.ocr_status == OcrStatus.pending
        assert receipt.processing_path is None
        assert receipt.extracted_fields is None
        assert receipt.field_confidence is None
        assert receipt.field_provenance is None
        assert receipt.grounding_context is None


# ---------------------------------------------------------------------------
# (i) MinIO: self-skipping receipt image fetch
# ---------------------------------------------------------------------------


def test_minio_receipt_images_fetchable(db_engine: Engine, seeded: dict) -> None:
    """Verify each seeded receipt image_url is reachable in MinIO.

    Self-skips if boto3 is not installed or MinIO is unreachable.
    """
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
    except ImportError:
        pytest.skip("boto3 not installed — skipping MinIO receipt image test")

    endpoint = os.environ.get("STORAGE_ENDPOINT_URL", "http://localhost:9000")
    bucket = os.environ.get("STORAGE_BUCKET", "atlas-receipts-dev")
    access_key = os.environ.get("STORAGE_ACCESS_KEY_ID", "minioadmin")
    secret_key = os.environ.get("STORAGE_SECRET_ACCESS_KEY", "")

    if not secret_key:
        pytest.skip("STORAGE_SECRET_ACCESS_KEY not set — skipping MinIO test")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("STORAGE_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )

    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        pytest.skip(f"MinIO bucket {bucket!r} not reachable — skipping MinIO test")

    from database.seeds.seed_all import _RECEIPT_KEYS  # noqa: PLC0415

    for key in _RECEIPT_KEYS:
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            data = resp["Body"].read()
            assert len(data) > 0, f"Empty image at key: {key}"
        except Exception as exc:
            pytest.fail(f"Failed to fetch receipt image {key!r} from MinIO: {exc}")
