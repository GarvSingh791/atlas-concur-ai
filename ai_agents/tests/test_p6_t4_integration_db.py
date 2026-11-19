"""P6-T4 integration tests — OCR, Inventory, Policy RAG adapters + M-R4 route mix.

Marked @pytest.mark.db — self-skips when Postgres is unreachable or
policy_embeddings is missing (run `alembic upgrade head` + seed first).

Adapter proofs:
  T4.1 TestReceiptAdapter   — OCR/receipt pipeline, clean vs low-conf paths
  T4.2 TestInventoryAdapter — mock inventory, SFO→JFK flight options + policy labels
  T4.3 TestPolicyRagAdapter — policy RAG, grounded vs silent paths via orchestrator
  T4.4 TestRouteMix         — 12-input M-R4 measurement: det-first + anti-cheat spy

Fix validated:
  LocalOcrProvider.extract(bytes) now decodes bytes to str before hint-matching so
  the clean-receipt deterministic path is reachable in local mode (P6-T4 finding).

Embedding hash note:
  LocalEmbeddingsProvider uses salted hash(); seed() is called in-process in the
  module-scoped `seeded` fixture so seed-time and query-time embeddings are
  hash-consistent within this interpreter session.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.orchestrator.envelopes import Task
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from ai_agents.tests._p6t4_support import SpyLlmProvider, build_spied_orchestrator, make_ctx
from database.models.ai_decision_log import AiAgent, AiDecisionLog, AiRoute
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.local import LocalStorageProvider

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures (follow the pattern in test_rag_retrieval_db.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping P6-T4 integration tests")

    insp = sa.inspect(engine)
    if "policy_embeddings" not in insp.get_table_names():
        pytest.skip("policy_embeddings table missing — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization, User  # noqa: PLC0415
    from database.models.policy import Policy  # noqa: PLC0415
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415
    from database.seeds.policy_content import CLAUSES  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415
    from services.embeddings_provider.local import embed  # noqa: PLC0415

    seed()  # idempotent; creates org/users/policy/rules/trips

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        employee = session.query(User).filter(User.email == "employee@atlas-demo.internal").one()
        policy = session.query(Policy).filter(Policy.organization_id == org.id).first()

        if policy is not None:
            # Re-create embeddings in this process so LocalEmbeddingsProvider hash-consistency
            # holds.  The seed is idempotent at the policy level; embeddings created by a prior
            # process have a different Python hash seed, so they must be replaced here.
            session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy.id).delete(
                synchronize_session=False
            )
            for clause in CLAUSES:
                session.add(
                    PolicyEmbedding(
                        policy_id=policy.id,
                        chunk_text=clause["chunk_text"],
                        clause_ref=clause["clause_ref"],
                        embedding=embed(clause["chunk_text"]),
                    )
                )
            session.commit()

    return {"org_id": org.id, "employee_id": employee.id}


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Receipt staging helper
# ---------------------------------------------------------------------------

_OCR = LocalOcrProvider()
_STORAGE = LocalStorageProvider()


def _stage_receipt(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    image_key: str,
    image_bytes: bytes,
    storage: LocalStorageProvider | None = None,
) -> Receipt:
    """Insert a pending Receipt row and stage image bytes in local storage."""
    store = storage or _STORAGE
    store.put_object(image_key, image_bytes)
    r = Receipt(
        organization_id=org_id,
        uploaded_by=user_id,
        image_url=image_key,
        ocr_status=OcrStatus.pending,
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# T4.1 — OCR / Receipt adapter
# ---------------------------------------------------------------------------


class TestReceiptAdapter:
    """Proves LocalOcrProvider.extract(bytes) resolves clean receipts deterministically."""

    def test_clean_airfare_deterministic(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])
        receipt = _stage_receipt(
            db_session,
            org_id=seeded["org_id"],
            user_id=seeded["employee_id"],
            image_key="receipts/test/t4-airfare.png",
            image_bytes=b"airfare receipt united airlines 450 usd",
        )

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=_OCR,
            llm=spy,
            storage=_STORAGE,
        )

        assert (
            receipt.processing_path == ProcessingPath.deterministic
        ), "Clean airfare receipt must resolve via the deterministic path after OCR fix"
        assert receipt.field_provenance is not None
        for field, prov in receipt.field_provenance.items():
            assert (
                prov == "deterministic"
            ), f"Field '{field}' has provenance '{prov}' — expected 'deterministic'"
        assert receipt.extracted_fields is not None
        assert receipt.extracted_fields.get("amount") == 450.0
        assert (
            spy.total_calls == 0
        ), f"NFR-P3 violation: {spy.total_calls} LLM calls on clean airfare receipt"

    def test_clean_hotel_deterministic(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])
        receipt = _stage_receipt(
            db_session,
            org_id=seeded["org_id"],
            user_id=seeded["employee_id"],
            image_key="receipts/test/t4-hotel.png",
            image_bytes=b"hotel marriott new york 210 usd",
        )

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=_OCR,
            llm=spy,
            storage=_STORAGE,
        )

        assert receipt.processing_path == ProcessingPath.deterministic
        assert receipt.extracted_fields is not None
        assert receipt.extracted_fields.get("amount") == 210.0
        assert (
            spy.total_calls == 0
        ), f"NFR-P3 violation: {spy.total_calls} LLM calls on clean hotel receipt"

    def test_clean_meal_deterministic(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])
        receipt = _stage_receipt(
            db_session,
            org_id=seeded["org_id"],
            user_id=seeded["employee_id"],
            image_key="receipts/test/t4-meal.png",
            image_bytes=b"meal restaurant chicago grille 42.50 usd",
        )

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=_OCR,
            llm=spy,
            storage=_STORAGE,
        )

        assert receipt.processing_path == ProcessingPath.deterministic
        assert receipt.extracted_fields is not None
        assert receipt.extracted_fields.get("amount") == 42.5
        assert (
            spy.total_calls == 0
        ), f"NFR-P3 violation: {spy.total_calls} LLM calls on clean meal receipt"

    def test_low_conf_routes_to_gap_fill(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])
        receipt = _stage_receipt(
            db_session,
            org_id=seeded["org_id"],
            user_id=seeded["employee_id"],
            image_key="receipts/test/t4-unknown.png",
            image_bytes=b"unrecognized image content t4 low confidence test",
        )

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=_OCR,
            llm=spy,
            storage=_STORAGE,
        )

        assert (
            receipt.processing_path == ProcessingPath.agent_gap_fill
        ), "Low-confidence receipt must route to agent_gap_fill"
        assert receipt.field_provenance is not None
        assert any(
            v == "ai_inferred" for v in receipt.field_provenance.values()
        ), "Gap-filled receipt must have at least one ai_inferred field"
        assert spy.total_calls >= 1, f"Expected ≥1 LLM call on gap-fill, got {spy.total_calls}"


# ---------------------------------------------------------------------------
# T4.2 — Mock inventory adapter
# ---------------------------------------------------------------------------


class TestInventoryAdapter:
    """Proves MockInventoryProvider returns correct options and policy labels for SFO→JFK."""

    def test_travel_sfo_jfk_inventory(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        orch = build_spied_orchestrator(spy)
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])

        task = Task(
            context_type="travel_planning",
            signals={},
            payload={
                "origin": "SFO",
                "destination": "JFK",
                "departure_date": "2026-06-10",
                "travelers": 1,
                "currency": "USD",
                "include_hotels": False,
            },
        )
        outcome = orch.handle_task(task, context=ctx, session=db_session)
        shortlist = outcome.result.output.get("shortlist", [])

        assert len(shortlist) > 0, "Expected at least one SFO→JFK flight option"

        # All option_ids must come from the seeded inventory — no fabrication
        option_ids = {o["option_id"] for o in shortlist}
        known_ids = {"UA201-2026-06-10", "DL408-2026-06-10", "UA201B-2026-06-10"}
        assert (
            option_ids <= known_ids
        ), f"shortlist contains fabricated option_ids not in inventory: {option_ids - known_ids}"

        # Out-of-policy business option must be present and labeled correctly
        ua201b = next((o for o in shortlist if o["option_id"] == "UA201B-2026-06-10"), None)
        assert ua201b is not None, "UA201B-2026-06-10 (business class) missing from shortlist"
        assert (
            ua201b["in_policy"] is False
        ), "Business class short-haul must be labeled out-of-policy"
        clause_refs = ua201b.get("clause_refs") or []
        assert (
            "AIR-CABIN" in clause_refs
        ), f"Business class option must cite AIR-CABIN; got clause_refs={clause_refs}"

        # At least one in-policy economy option
        assert any(o["in_policy"] for o in shortlist), "Expected at least one in-policy flight"

        # Ranking: in-policy options always rank above out-of-policy (+100 score gap)
        assert (
            shortlist[0]["in_policy"] is True
        ), "Out-of-policy option must never be top-ranked (ranking score invariant)"

        # Anti-cheat: no LLM calls on the deterministic travel pipeline
        assert spy.total_calls == 0, (
            f"NFR-P3 violation: {spy.total_calls} LLM calls on travel task"
            " with explain_with_llm=False"
        )


# ---------------------------------------------------------------------------
# T4.3 — Policy RAG adapter
# ---------------------------------------------------------------------------


class TestPolicyRagAdapter:
    """Proves GroundedRetriever grounded and silent paths through the orchestrator."""

    def test_grounded_via_orchestrator(self, seeded: dict, db_session: Session) -> None:
        """On-policy question → GroundedRetriever returns grounded → PolicyAgent calls LLM."""
        spy = SpyLlmProvider()
        orch = build_spied_orchestrator(spy)
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])

        task = Task(
            context_type="policy_qa",
            signals={},
            payload={
                "question": (
                    "flight cabin class economy business premium duration scheduled hours upgrade"
                )
            },
        )
        outcome = orch.handle_task(task, context=ctx, session=db_session)
        output = outcome.result.output

        assert output.get("answer_type") in {"grounded", "needs_review"}, (
            f"On-policy query must return grounded or needs_review; "
            f"got answer_type={output.get('answer_type')!r}"
        )
        assert (
            output.get("answer_type") != "silent"
        ), "On-policy cabin-class query must not be silenced by the threshold guardrail"
        assert spy.complete_calls >= 2, (
            f"Grounded path must invoke LLM for hypothesizer + critic; "
            f"got complete_calls={spy.complete_calls}"
        )

    def test_silent_via_orchestrator(self, seeded: dict, db_session: Session) -> None:
        """Off-topic question → GroundedRetriever returns silent → PolicyAgent makes 0 LLM calls.

        Uses a strongly-orthogonal query: under the corpus + local embedder, broad
        off-topic abstention is weak (loosely-worded off-topic can hash-collide above
        the 0.20 gate) — calibrated abstention is R3.
        """
        spy = SpyLlmProvider()
        orch = build_spied_orchestrator(spy)
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])

        task = Task(
            context_type="policy_qa",
            signals={},
            payload={"question": "photosynthesis chlorophyll sunlight plant cells biology"},
        )
        outcome = orch.handle_task(task, context=ctx, session=db_session)
        output = outcome.result.output

        assert output.get("answer_type") == "silent", (
            f"Off-topic query must return answer_type=silent; " f"got {output.get('answer_type')!r}"
        )
        assert output.get("answer") is None, "Silent answer must be None"
        assert spy.total_calls == 0, (
            f"NFR-P3 + silent guardrail: 0 LLM calls expected on silent path, "
            f"got {spy.total_calls}"
        )


# ---------------------------------------------------------------------------
# T4.4 — M-R4 route mix (12-input characterized set)
# ---------------------------------------------------------------------------

# Expected (agent.value, task_type, route.value) multiset across 12 inputs.
# Derivation: per-input routing is annotated inline below; the 12-input set and its
# 8 deterministic / 4 llm split are recorded in docs/phase_logs/Phase6_P6-T4.md.
_EXPECTED_ROWS: Counter = Counter(
    {
        ("orchestrator", "policy_evaluate", "deterministic"): 4,  # inputs 1,2,11,12
        ("policy", "policy_evaluate", "deterministic"): 4,  # PolicyEvalHandler: 1,2,11,12
        ("orchestrator", "expense_categorize_simple", "deterministic"): 1,  # input 3
        ("expense", "expense_categorize_simple", "deterministic"): 1,  # C1 handler: input 3
        ("receipt_analysis", "ocr_extraction", "deterministic"): 3,  # inputs 4,5,6
        ("orchestrator", "policy_grounded_lookup", "llm"): 2,  # inputs 7,8
        ("policy", "policy_grounded_lookup", "llm"): 2,  # PolicyAgent: 7,8
        ("orchestrator", "trip_planning", "llm"): 1,  # input 9
        ("travel", "trip_planning", "deterministic"): 1,  # TravelAgent: input 9
        ("receipt_analysis", "gap_fill", "llm"): 1,  # input 10
    }
)


class TestRouteMix:
    """Drives all 12 characterized inputs and verifies the M-R4 route distribution.

    Anti-cheat: a single SpyLlmProvider is shared across all inputs.  Each
    deterministic input is asserted to add a zero delta to spy.total_calls.
    Only inputs #7 (grounded NL) and #10 (receipt gap-fill) may call the LLM.
    """

    def test_route_mix_deterministic_first(self, seeded: dict, db_session: Session) -> None:
        spy = SpyLlmProvider()
        orch = build_spied_orchestrator(spy)
        ctx = make_ctx(seeded["org_id"], user_id=seeded["employee_id"])
        ocr = LocalOcrProvider()
        storage = LocalStorageProvider()

        # Snapshot existing committed row IDs so we can scope to only this run's rows.
        # Other test modules commit ai_decision_logs rows for the same org; we must exclude them.
        existing_ids = frozenset(
            db_session.scalars(
                sa.select(AiDecisionLog.id).where(AiDecisionLog.organization_id == seeded["org_id"])
            ).all()
        )

        def _policy_eval(payload: dict) -> None:
            orch.handle_task(
                Task(context_type="policy_qa", signals={"policy_eval": True}, payload=payload),
                context=ctx,
                session=db_session,
            )

        def _receipt(image_key: str, image_bytes: bytes) -> None:
            r = _stage_receipt(
                db_session,
                org_id=seeded["org_id"],
                user_id=seeded["employee_id"],
                image_key=image_key,
                image_bytes=image_bytes,
                storage=storage,
            )
            run_receipt_pipeline(
                db_session,
                context=ctx,
                receipt_id=r.id,
                ocr=ocr,
                llm=spy,
                storage=storage,
            )

        # ── 1: policy eval in-policy flight (economy 3h, $320) ──────────────
        before = spy.total_calls
        _policy_eval(
            {
                "item_type": "flight",
                "cabin_class": "economy",
                "duration_hours": 3.0,
                "amount_usd": 320.0,
            }
        )
        assert (
            spy.total_calls == before
        ), f"#1 policy_eval: unexpected LLM delta {spy.total_calls - before}"

        # ── 2: policy eval out-of-policy hotel ($300 Austin) ─────────────────
        before = spy.total_calls
        _policy_eval({"item_type": "hotel", "amount_usd": 300.0, "city": "Austin"})
        assert (
            spy.total_calls == before
        ), f"#2 policy_eval: unexpected LLM delta {spy.total_calls - before}"

        # ── 3: expense simple (ExpenseCategorizeHandler: "taxi" keyword → ─────
        #       ground_transport at 0.80 ≥ threshold → deterministic, 0 LLM) ──
        before = spy.total_calls
        simple_outcome = orch.handle_task(
            Task(
                context_type="expense",
                signals={"expense_simple": True},
                payload={"description": "taxi", "amount": 25.0},
            ),
            context=ctx,
            session=db_session,
        )
        assert (
            spy.total_calls == before
        ), f"#3 expense_simple: unexpected LLM delta {spy.total_calls - before}"
        assert simple_outcome.result.output.get("category") == "ground_transport"
        assert simple_outcome.result.output.get("route") == "deterministic"

        # ── 4: receipt clean airfare ─────────────────────────────────────────
        before = spy.total_calls
        _receipt("receipts/test/mix-airfare.png", b"airfare receipt united airlines 450")
        assert (
            spy.total_calls == before
        ), f"#4 clean airfare: unexpected LLM delta {spy.total_calls - before}"

        # ── 5: receipt clean hotel ───────────────────────────────────────────
        before = spy.total_calls
        _receipt("receipts/test/mix-hotel.png", b"hotel marriott new york stay 210")
        assert (
            spy.total_calls == before
        ), f"#5 clean hotel: unexpected LLM delta {spy.total_calls - before}"

        # ── 6: receipt clean meal ────────────────────────────────────────────
        before = spy.total_calls
        _receipt("receipts/test/mix-meal.png", b"meal restaurant chicago grille 42")
        assert (
            spy.total_calls == before
        ), f"#6 clean meal: unexpected LLM delta {spy.total_calls - before}"

        # ── 7: policy NL grounded (cabin-class query → LLM synthesis) ──────
        before = spy.total_calls
        orch.handle_task(
            Task(
                context_type="policy_qa",
                signals={},
                payload={
                    "question": (
                        "flight cabin class economy business premium "
                        "duration scheduled hours upgrade"
                    )
                },
            ),
            context=ctx,
            session=db_session,
        )
        calls_7 = spy.total_calls - before
        assert (
            calls_7 >= 2
        ), f"#7 policy grounded: expected ≥2 LLM calls (hypothesizer+critic), got {calls_7}"

        # ── 8: policy NL silent (off-topic → 0 LLM calls) ────────────────────
        before = spy.total_calls
        orch.handle_task(
            Task(
                context_type="policy_qa",
                signals={},
                payload={"question": "photosynthesis chlorophyll sunlight plant cells biology"},
            ),
            context=ctx,
            session=db_session,
        )
        assert spy.total_calls == before, (
            f"#8 policy silent: unexpected LLM delta {spy.total_calls - before} "
            "(silent guardrail must short-circuit before LLM)"
        )

        # ── 9: travel SFO→JFK (deterministic pipeline, 0 LLM) ────────────────
        before = spy.total_calls
        orch.handle_task(
            Task(
                context_type="travel_planning",
                signals={},
                payload={
                    "origin": "SFO",
                    "destination": "JFK",
                    "departure_date": "2026-06-10",
                    "travelers": 1,
                    "currency": "USD",
                    "include_hotels": False,
                },
            ),
            context=ctx,
            session=db_session,
        )
        assert spy.total_calls == before, (
            f"#9 travel: unexpected LLM delta {spy.total_calls - before} "
            "(travel pipeline is deterministic when explain_with_llm=False)"
        )

        # ── 10: receipt low-conf (gap_fill → ≥1 LLM call) ────────────────────
        before = spy.total_calls
        _receipt("receipts/test/mix-unknown.png", b"unrecognized content p6t4 mix test")
        calls_10 = spy.total_calls - before
        assert (
            calls_10 >= 1
        ), f"#10 low-conf receipt: expected ≥1 LLM call on gap_fill, got {calls_10}"

        # ── 11: policy eval out-of-policy car luxury ──────────────────────────
        before = spy.total_calls
        _policy_eval({"item_type": "car", "vehicle_class": "luxury", "amount_usd": 500.0})
        assert (
            spy.total_calls == before
        ), f"#11 policy_eval car: unexpected LLM delta {spy.total_calls - before}"

        # ── 12: policy eval receipt $10 airfare ───────────────────────────────
        before = spy.total_calls
        _policy_eval({"item_type": "receipt", "amount_usd": 10.0, "expense_category": "airfare"})
        assert (
            spy.total_calls == before
        ), f"#12 policy_eval receipt: unexpected LLM delta {spy.total_calls - before}"

        # ── Query only this run's rows (flushed, same transaction; exclude pre-existing) ─
        all_rows = db_session.scalars(
            sa.select(AiDecisionLog)
            .where(AiDecisionLog.organization_id == seeded["org_id"])
            .order_by(AiDecisionLog.created_at.asc())
        ).all()
        rows = [r for r in all_rows if r.id not in existing_ids]

        observed: Counter = Counter((r.agent.value, r.task_type, r.route.value) for r in rows)
        assert observed == _EXPECTED_ROWS, (
            "M-R4 row multiset mismatch.\n"
            f"  Expected : {dict(sorted(_EXPECTED_ROWS.items()))}\n"
            f"  Observed : {dict(sorted(observed.items()))}\n"
            f"  Missing  : {dict((_EXPECTED_ROWS - observed).items())}\n"
            f"  Unexpected: {dict((observed - _EXPECTED_ROWS).items())}"
        )

        # ── M-R4 routing writers: orchestrator + receipt_analysis ─────────────
        routing_rows = [
            r for r in rows if r.agent in {AiAgent.orchestrator, AiAgent.receipt_analysis}
        ]
        det_count = sum(1 for r in routing_rows if r.route == AiRoute.deterministic)
        llm_count = sum(1 for r in routing_rows if r.route == AiRoute.llm)
        assert det_count > llm_count, (
            f"M-R4 must be deterministic-first: det={det_count}, llm={llm_count} "
            f"(expected 8 det / 4 llm across {len(routing_rows)} routing rows)"
        )
        assert det_count == 8, f"Expected 8 deterministic routing rows, got {det_count}"
        assert llm_count == 4, f"Expected 4 llm routing rows, got {llm_count}"

        # ── Anti-cheat summary: LLM calls attributable to #7 + #10 only ───────
        total_llm = spy.total_calls
        expected_min = calls_7 + calls_10
        assert total_llm == expected_min, (
            f"Total LLM calls ({total_llm}) must equal #7 calls ({calls_7}) + "
            f"#10 calls ({calls_10}).  Extra calls detected on a 'deterministic' input."
        )
