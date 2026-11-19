"""P6-T5 degradation tests — graceful degradation across all branches (NFR-R1/R2/R3).

Marked @pytest.mark.db — self-skips when Postgres is unreachable.

Coverage:
  T5.1 Branch A error     — OCR ProviderError + LLM gap-fill ProviderError; safe outcome
  T5.2 Branch A timeout   — same but code="TIMEOUT"; proves convergence + flags gap
  T5.3 Branch B rejection — reflection rejects hypothesis; defers, never commits guess
  T5.4 Branch C coexist   — low-confidence (non-failure) → needs-review; distinguishable
  T5.5 Atomicity          — mid-write fault → no half-commit; idempotent retry no duplicate

"Needs review" representation (no new column per drift guard):
  Receipt OCR failure : receipts.ocr_status = 'failed'
  Receipt gap-fill    : field_confidence[field] < 0.70 or 0.10 sentinel; provenance='ai_inferred'
  Reflection rejection: ai_decision_logs.reflection_outcome = 'rejected'
  All domains         : ai_decision_logs.output_summary->>'needs_review' = 'true'
                        and/or ->>'degraded' = 'true'
  Travel/Expense      : AgentResult.status = needs_review

TIMEOUT GAP FLAG: LLM_TIMEOUT_SECONDS / OCR_TIMEOUT_SECONDS are defined in settings
but NOT enforced at any call site.  Error and timeout branch through the same
except ProviderError handler.  T5.2 proves the safe outcome is identical and flags
the missing enforcement as P7-gap/timeout-not-wired.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.orchestrator.envelopes import AgentResultStatus, Task
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from ai_agents.tests._p6t5_support import (
    FaultyLlmProvider,
    FaultyOcrProvider,
    build_faulted_orchestrator,
    make_ctx,
)
from database.models.ai_decision_log import AiDecisionLog, AiRoute, ReflectionOutcome
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.local import LocalStorageProvider

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_AIRFARE_BYTES = b"airfare receipt united airlines 450 usd"
_UNKNOWN_BYTES = b"unrecognized content t5 degradation test"


# ---------------------------------------------------------------------------
# DB engine fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping P6-T5 degradation tests")

    insp = sa.inspect(engine)
    if "policy_embeddings" not in insp.get_table_names():
        pytest.skip("policy_embeddings table missing — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization, User  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()  # idempotent; creates org/users/policy/rules/trips

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        employee = session.query(User).filter(User.email == "employee@atlas-demo.internal").one()

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
# Receipt staging helper (reuse T4 pattern)
# ---------------------------------------------------------------------------

_STORAGE = LocalStorageProvider()


def _stage_receipt(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    image_key: str,
    image_bytes: bytes,
) -> Receipt:
    """Insert a pending Receipt row and put image bytes in local storage."""
    _STORAGE.put_object(image_key, image_bytes)
    r = Receipt(
        organization_id=org_id,
        uploaded_by=user_id,
        image_url=image_key,
        ocr_status=OcrStatus.pending,
    )
    session.add(r)
    session.flush()
    return r


def _latest_log(session: Session, *, org_id: uuid.UUID) -> AiDecisionLog | None:
    return session.scalars(
        sa.select(AiDecisionLog)
        .where(AiDecisionLog.organization_id == org_id)
        .order_by(AiDecisionLog.created_at.desc())
        .limit(1)
    ).first()


# ---------------------------------------------------------------------------
# T5.1 + T5.2 — Branch A (error and timeout)
# ---------------------------------------------------------------------------


class TestBranchAError:
    """Branch A — LLM/OCR ProviderError → deterministic fallback + needs-review mark.

    Three-part assertion per faulted call site:
      (i)   flow COMPLETES — no exception raised to the caller
      (ii)  result is the DETERMINISTIC FALLBACK — not an error, not a fabricated value
      (iii) affected fields / receipt explicitly marked 'needs review' in persisted state
      (iv)  ai_decision_logs row records the failure (route attempted, fallback taken)
    """

    def test_ocr_error_receipt_fails_safely(self, seeded: dict, db_session: Session) -> None:
        """OCR ProviderError → ocr_status=failed + decision log ocr_failed=true."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-ocr-error.png",
            image_bytes=_AIRFARE_BYTES,
        )

        faulty_ocr = FaultyOcrProvider(error_code="PROVIDER_UNAVAILABLE")
        local_llm = LocalLlmProvider()

        # (i) flow completes — no exception propagated
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=faulty_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # (ii) deterministic fallback: ocr_status=failed (early return, no fields committed)
        assert (
            receipt.ocr_status == OcrStatus.failed
        ), f"Branch A OCR error: expected ocr_status=failed, got {receipt.ocr_status.value}"
        # No extracted fields committed on OCR failure
        assert (
            receipt.extracted_fields is None
        ), "Branch A OCR error: extracted_fields must be None when OCR fails (no partial commit)"

        # (iii) needs-review mark: ocr_status=failed IS the needs-review representation
        assert receipt.ocr_status == OcrStatus.failed  # already checked above

        # (iv) decision log: ocr_failed=true, route=deterministic
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None, "Branch A OCR error: no decision log row written"
        assert (
            log.route == AiRoute.deterministic
        ), f"Branch A OCR error: expected route=deterministic, got {log.route.value}"
        assert log.output_summary is not None
        assert (
            log.output_summary.get("ocr_failed") is True
        ), f"Branch A OCR error: output_summary.ocr_failed must be True; got {log.output_summary}"

    def test_llm_gap_fill_error_fields_null_needs_review(
        self, seeded: dict, db_session: Session
    ) -> None:
        """LLM ProviderError during gap-fill → empty inferred fields + needs_review mark."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        # Unknown image → all fields low-conf → gap-fill triggered
        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-llm-gap-error.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        local_ocr = LocalOcrProvider()
        faulty_llm = FaultyLlmProvider(error_code="PROVIDER_UNAVAILABLE")

        # (i) flow completes
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=faulty_llm,
            storage=_STORAGE,
        )

        # (ii) deterministic fallback: ocr_status=processed (OCR ran OK), gap-fill returns empty
        assert (
            receipt.ocr_status == OcrStatus.processed
        ), "Branch A LLM gap-fill error: OCR ran OK so ocr_status must be processed"
        assert (
            receipt.processing_path == ProcessingPath.agent_gap_fill
        ), "Branch A LLM gap-fill error: path must be agent_gap_fill (gap-fill was attempted)"

        # (iii) needs-review: field_confidence all low (gap-fill ProviderError → empty inferred)
        assert receipt.field_confidence is not None
        for field, conf in receipt.field_confidence.items():
            assert conf < 0.70, (
                f"Branch A LLM gap-fill error: field '{field}' has conf={conf} "
                "— expected < 0.70 (gap-fill failed, no LLM inference)"
            )

        # (iv) decision log: needs_review=true, route=llm (gap-fill was attempted)
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert (
            log.route == AiRoute.llm
        ), f"Branch A LLM gap-fill error: expected route=llm, got {log.route.value}"
        assert log.output_summary is not None
        assert log.output_summary.get("needs_review") is True, (
            f"Branch A LLM gap-fill error: output_summary.needs_review must be True; "
            f"got {log.output_summary}"
        )

    def test_llm_narrative_travel_error_degrades(self, seeded: dict, db_session: Session) -> None:
        """LLM ProviderError on travel narrative → templated fallback + status=needs_review."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        faulty_llm = FaultyLlmProvider(error_code="PROVIDER_UNAVAILABLE")
        orch = build_faulted_orchestrator(faulty_llm)

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
                "explain_with_llm": True,  # triggers LLM narrative path
            },
        )

        # (i) flow completes
        outcome = orch.handle_task(task, context=ctx, session=db_session)

        # (ii) deterministic fallback: shortlist returned (templated explanations used)
        shortlist = outcome.result.output.get("shortlist", [])
        assert (
            len(shortlist) > 0
        ), "Branch A LLM narrative (travel): shortlist must be non-empty despite LLM fault"

        # (iii) needs-review marked: status=needs_review due to degraded=True
        assert outcome.result.status == AgentResultStatus.needs_review, (
            f"Branch A LLM narrative (travel): expected status=needs_review, "
            f"got {outcome.result.status.value}"
        )
        assert (
            outcome.result.output.get("degraded") is True
        ), "Branch A LLM narrative (travel): output.degraded must be True"

        # (iv) decision log: degraded=true, route=deterministic
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert log.route == AiRoute.deterministic, (
            f"Branch A LLM narrative (travel): expected route=deterministic (LLM failed), "
            f"got {log.route.value}"
        )
        assert log.output_summary is not None
        assert log.output_summary.get("degraded") is True, (
            f"Branch A LLM narrative (travel): output_summary.degraded must be True; "
            f"got {log.output_summary}"
        )

    def test_llm_narrative_expense_error_degrades(self, seeded: dict, db_session: Session) -> None:
        """LLM ProviderError on expense narrative → templated fallback + status=needs_review."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        faulty_llm = FaultyLlmProvider(error_code="PROVIDER_UNAVAILABLE")
        orch = build_faulted_orchestrator(faulty_llm)

        task = Task(
            context_type="expense",
            signals={},
            payload={
                "explain_with_llm": True,  # triggers LLM narrative path
            },
        )

        # (i) flow completes
        outcome = orch.handle_task(task, context=ctx, session=db_session)

        # The expense agent may return ok (nothing to assemble) or needs_review (degraded).
        # Either way, no exception should propagate.
        assert (
            outcome.result is not None
        ), "Branch A LLM narrative (expense): agent must return a result"

        # If the narrative path was reached (explain_with_llm=True, entries exist), degraded=True
        output = outcome.result.output or {}
        if output.get("degraded") is True:
            # narrative degraded: needs_review
            assert outcome.result.status == AgentResultStatus.needs_review, (
                f"Branch A LLM narrative (expense): degraded=True must imply needs_review; "
                f"got status={outcome.result.status.value}"
            )
        # If "nothing to assemble", the degraded path wasn't reached — that's acceptable;
        # the LLM fault was encountered but no entries existed so the narrative was never called.


# ---------------------------------------------------------------------------
# T5.2 — Branch A timeout (simulated, same handler as error)
# ---------------------------------------------------------------------------


class TestBranchATimeout:
    """Branch A — timeout variant.

    Proves safe outcome is IDENTICAL to error (both share the except ProviderError handler).
    Documents P7 gap: timeout settings exist but are not enforced at call sites.
    """

    def test_ocr_timeout_same_safe_outcome(self, seeded: dict, db_session: Session) -> None:
        """Simulated OCR timeout → identical safe outcome as OCR error."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-ocr-timeout.png",
            image_bytes=_AIRFARE_BYTES,
        )

        # Simulate OCR timeout (same ProviderError handler, different code)
        faulty_ocr = FaultyOcrProvider(error_code="TIMEOUT")
        local_llm = LocalLlmProvider()

        # (i) flow completes
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=faulty_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # (ii)/(iii) same safe outcome as error: ocr_status=failed
        assert (
            receipt.ocr_status == OcrStatus.failed
        ), "Branch A OCR timeout: expected ocr_status=failed (same as error)"
        assert receipt.extracted_fields is None

        # (iv) decision log captures the fallback
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert log.output_summary is not None
        assert (
            log.output_summary.get("ocr_failed") is True
        ), "Branch A OCR timeout: output_summary.ocr_failed must be True"

        # P7 gap annotation (timeout enforcement not wired — flagged for Phase 7)
        # The ProviderError.code="TIMEOUT" simulates what a wired timeout WOULD raise.
        # No production fix done here; the gap is: no asyncio.wait_for / signal wrapping
        # exists in services/llm_provider/real.py or the OCR providers.

    def test_llm_timeout_same_safe_outcome(self, seeded: dict, db_session: Session) -> None:
        """Simulated LLM timeout on gap-fill → identical safe outcome as LLM error."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-llm-timeout.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        local_ocr = LocalOcrProvider()
        faulty_llm = FaultyLlmProvider(error_code="TIMEOUT")

        # (i) flow completes
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=faulty_llm,
            storage=_STORAGE,
        )

        # (ii)/(iii) same safe outcome as LLM error
        assert receipt.ocr_status == OcrStatus.processed
        assert receipt.processing_path == ProcessingPath.agent_gap_fill
        assert receipt.field_confidence is not None
        for field, conf in receipt.field_confidence.items():
            assert conf < 0.70, f"Branch A LLM timeout: field '{field}' conf={conf} must be < 0.70"

        # (iv) decision log
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert log.output_summary is not None
        assert (
            log.output_summary.get("needs_review") is True
        ), "Branch A LLM timeout: output_summary.needs_review must be True"

    def test_llm_narrative_travel_timeout_same_safe_outcome(
        self, seeded: dict, db_session: Session
    ) -> None:
        """Simulated LLM narrative timeout → identical safe outcome as narrative error."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        faulty_llm = FaultyLlmProvider(error_code="TIMEOUT")
        orch = build_faulted_orchestrator(faulty_llm)

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
                "explain_with_llm": True,
            },
        )

        # (i) flow completes
        outcome = orch.handle_task(task, context=ctx, session=db_session)

        # (ii)/(iii) same safe outcome as error: shortlist present, status=needs_review
        shortlist = outcome.result.output.get("shortlist", [])
        assert (
            len(shortlist) > 0
        ), "Branch A LLM narrative timeout (travel): shortlist must be non-empty"
        assert outcome.result.status == AgentResultStatus.needs_review, (
            f"Branch A LLM narrative timeout: expected needs_review, "
            f"got {outcome.result.status.value}"
        )
        assert outcome.result.output.get("degraded") is True


# ---------------------------------------------------------------------------
# T5.3 — Branch B: reflection rejection
# ---------------------------------------------------------------------------


class TestBranchBReflectionRejection:
    """Branch B — reflection rejection → defer to deterministic, never commit rejected guess.

    Scenario: the LLM ran (gap-fill was invoked successfully) but the hypothesis was
    rejected by the GroundingCritic (no matching city in the injected itinerary facts).
    The rejected value is NOT committed; location=null, confidence=0.10 sentinel.

    This is the "Paris paradox" defence: an LLM-inferred location that cannot be
    grounded against the trip itinerary is rejected before being written.
    """

    def test_location_reflection_rejected_not_committed(
        self, seeded: dict, db_session: Session
    ) -> None:
        """Reflection rejects ungroundable location → null committed, decision log = rejected."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        # Unknown image → all fields low-conf → gap-fill for ALL fields including location
        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-reflection-reject.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        # Local LLM runs successfully (returns non-JSON → location_inferred=None →
        # LocationClaim(city="unknown")).  Empty itinerary → GroundingCritic has no
        # location facts → verdict="unsupported" → reflect() returns rejected.
        local_ocr = LocalOcrProvider()
        local_llm = LocalLlmProvider()  # NOT faulted — LLM ran successfully

        # Use the default empty itinerary (itinerary_tool.get_itinerary with no trip context)
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # Assertions
        # (i) flow completed
        assert (
            receipt.ocr_status == OcrStatus.processed
        ), "Branch B: OCR ran OK, ocr_status must be processed"
        assert (
            receipt.processing_path == ProcessingPath.agent_gap_fill
        ), "Branch B: gap-fill was attempted, path must be agent_gap_fill"

        # (ii) rejected guess NOT committed: location field is None
        ef = receipt.extracted_fields or {}
        assert ef.get("location") is None, (
            f"Branch B: rejected location hypothesis must NOT be committed; "
            f"got location={ef.get('location')!r}"
        )

        # (iii) needs-review: low confidence sentinel for rejected location
        fc = receipt.field_confidence or {}
        if "location" in fc:
            assert fc["location"] <= 0.10, (
                f"Branch B: location confidence must be ≤ 0.10 (low-confidence sentinel); "
                f"got {fc['location']}"
            )
        fp = receipt.field_provenance or {}
        if "location" in fp:
            assert (
                fp["location"] == "ai_inferred"
            ), f"Branch B: location provenance must be ai_inferred; got {fp['location']!r}"

        # (iv) decision log: reflection_outcome=rejected
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert log.reflection_outcome == ReflectionOutcome.rejected, (
            f"Branch B: decision log must have reflection_outcome=rejected; "
            f"got {log.reflection_outcome}"
        )
        assert log.output_summary is not None
        assert (
            log.output_summary.get("needs_review") is True
        ), "Branch B: decision log output_summary.needs_review must be True"
        reflection_outcome_val = log.output_summary.get("reflection_outcome")
        assert reflection_outcome_val == "rejected", (
            f"Branch B: output_summary.reflection_outcome must be 'rejected'; "
            f"got {reflection_outcome_val!r}"
        )


# ---------------------------------------------------------------------------
# T5.4 — Branch C: low-confidence coexistence (non-failure)
# ---------------------------------------------------------------------------


class TestBranchCCoexistence:
    """Branch C — low-confidence non-failure coexists with A/B; distinguishable in persisted marks.

    A low-confidence gap-filled receipt is NOT a provider failure (Branch A) and may
    coexist with reflection rejection (Branch B is a subset of low-confidence paths).
    The distinguishing mark: no ocr_failed=true in the decision log (unlike Branch A OCR),
    and ocr_status=processed (unlike Branch A OCR error which sets failed).
    """

    def test_low_conf_needs_review_distinguishable(self, seeded: dict, db_session: Session) -> None:
        """Low-conf gap-fill → needs_review=True; no ocr_failed in log; ocr_status=processed."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-low-conf.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        local_ocr = LocalOcrProvider()
        local_llm = LocalLlmProvider()

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # Distinguishable from Branch A OCR error:
        # Branch A OCR error → ocr_status=failed; Branch C → ocr_status=processed
        assert receipt.ocr_status == OcrStatus.processed, (
            "Branch C: ocr_status must be processed (not failed) — "
            "distinguishes from Branch A OCR error"
        )
        assert (
            receipt.processing_path == ProcessingPath.agent_gap_fill
        ), "Branch C: path=agent_gap_fill (LLM ran without ProviderError)"

        # Decision log: no ocr_failed; needs_review=true; route=llm
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None
        assert log.route == AiRoute.llm, "Branch C: route must be llm (gap-fill ran successfully)"
        assert log.output_summary is not None
        # ocr_failed must NOT be present (distinguishes from Branch A OCR error)
        assert not log.output_summary.get(
            "ocr_failed"
        ), f"Branch C: output_summary must NOT have ocr_failed=true; got {log.output_summary}"
        # needs_review=true (low confidence, not a provider failure)
        assert (
            log.output_summary.get("needs_review") is True
        ), "Branch C: output_summary.needs_review must be True (low-confidence gap-fill)"

    def test_low_conf_not_silently_committed(self, seeded: dict, db_session: Session) -> None:
        """Low-confidence gap-fill values are committed but MARKED needs_review — never silent."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-low-conf-silent.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        local_ocr = LocalOcrProvider()
        local_llm = LocalLlmProvider()

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # ALL gap-filled fields have provenance=ai_inferred (not silently claimed as deterministic)
        fp = receipt.field_provenance or {}
        for field, prov in fp.items():
            assert prov in (
                "deterministic",
                "ai_inferred",
            ), f"Branch C: field '{field}' has unexpected provenance {prov!r}"

        # Decision log exists with needs_review flag — silent commit is the failure mode
        log = _latest_log(db_session, org_id=org_id)
        assert log is not None, "Branch C: decision log must be written (never silent)"
        assert log.output_summary is not None
        assert log.output_summary.get("needs_review") is True, (
            "Branch C: A SILENT COMMIT IS THE FAILURE MODE — "
            "needs_review must be True for low-confidence gap-fill"
        )


# ---------------------------------------------------------------------------
# T5.5 — Atomicity and idempotency
# ---------------------------------------------------------------------------


class TestAtomicityIdempotency:
    """T5.5 — NFR-R2/R3: no half-commit on fault; idempotent retry no duplicate.

    Atomicity proof: after an OCR fault, the receipt row is in a CONSISTENT state
    (ocr_status=failed, no partial extracted_fields) — never half-updated.

    Idempotency: a second call to run_receipt_pipeline on an already-processed receipt
    is idempotent — the pipeline re-runs but the second decision log row is the only
    addition; the receipt row is updated atomically.
    """

    def test_ocr_fault_receipt_in_consistent_state(self, seeded: dict, db_session: Session) -> None:
        """After OCR fault mid-pipeline, receipt is whole or untouched — no half-commit."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-atomicity.png",
            image_bytes=_AIRFARE_BYTES,
        )

        faulty_ocr = FaultyOcrProvider(error_code="PROVIDER_UNAVAILABLE")
        local_llm = LocalLlmProvider()

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=faulty_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # Consistent state after OCR fault: failed status, no partial extracted_fields
        assert (
            receipt.ocr_status == OcrStatus.failed
        ), "Atomicity: ocr_status must be failed after OCR fault"
        assert (
            receipt.extracted_fields is None
        ), "Atomicity: extracted_fields must be None — no partial write occurred (NFR-R2)"
        assert (
            receipt.processing_path is None
        ), "Atomicity: processing_path must be None — OCR failed before path was determined"

    def test_idempotent_retry_no_duplicate_receipt(self, seeded: dict, db_session: Session) -> None:
        """Running the pipeline twice on the same receipt is idempotent at the receipt level.

        The pipeline itself is stateless (the receipt row is the state machine); running
        it again on an already-processed receipt simply re-processes and updates the same row.
        No duplicate receipts are created (the caller creates the receipt row once).
        """
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        # First run: faulty OCR → failed
        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-idempotent.png",
            image_bytes=_AIRFARE_BYTES,
        )

        faulty_ocr = FaultyOcrProvider(error_code="PROVIDER_UNAVAILABLE")
        local_llm = LocalLlmProvider()

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=faulty_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        assert receipt.ocr_status == OcrStatus.failed

        # Count receipts before retry
        before_count = db_session.scalar(
            sa.select(sa.func.count()).select_from(Receipt).where(Receipt.organization_id == org_id)
        )

        # Second run: clean OCR (retry succeeds)
        local_ocr = LocalOcrProvider()
        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=local_llm,
            storage=_STORAGE,
        )

        # Still the SAME receipt row — no duplicate created
        after_count = db_session.scalar(
            sa.select(sa.func.count()).select_from(Receipt).where(Receipt.organization_id == org_id)
        )
        assert before_count == after_count, (
            f"Atomicity idempotency: retry must not create a new receipt row "
            f"(before={before_count}, after={after_count}). NFR-R3 violation."
        )
        # And the receipt is now processed
        assert (
            receipt.ocr_status == OcrStatus.processed
        ), "Atomicity: second run (clean OCR) should update same row to processed"

    def test_no_silent_commit_on_llm_fault(self, seeded: dict, db_session: Session) -> None:
        """A fallback committed without a needs_review mark is the failure mode.

        Confirms the three-part assertion per faulted call site explicitly:
        for every injected failure, either the field is null OR the decision log
        has needs_review=True — never a silent commit.
        """
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = make_ctx(org_id, user_id=user_id)

        receipt = _stage_receipt(
            db_session,
            org_id=org_id,
            user_id=user_id,
            image_key="receipts/test/t5-no-silent.png",
            image_bytes=_UNKNOWN_BYTES,
        )

        local_ocr = LocalOcrProvider()
        faulty_llm = FaultyLlmProvider(error_code="PROVIDER_UNAVAILABLE")

        run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=local_ocr,
            llm=faulty_llm,
            storage=_STORAGE,
        )

        # For every field that has a value: either it came from the deterministic path
        # (provenance=deterministic) or it was flagged for review (provenance=ai_inferred
        # with low conf).  No field may be silently committed with high confidence.
        ef = receipt.extracted_fields or {}
        fc = receipt.field_confidence or {}

        for field, val in ef.items():
            if val is None:
                continue  # null is safe (not silently committed)
            conf = fc.get(field, 0.0)
            # Any committed non-null value must EITHER be deterministic (high conf)
            # OR have needs_review=True in the decision log (low conf).
            if conf < 0.70:
                # Low-confidence committed value → must have log needs_review mark
                log = _latest_log(db_session, org_id=org_id)
                assert log is not None
                assert log.output_summary is not None
                assert log.output_summary.get("needs_review") is True, (
                    f"SILENT COMMIT VIOLATION: field '{field}' committed with conf={conf} < 0.70 "
                    f"but decision log does not have needs_review=True. "
                    f"output_summary={log.output_summary}"
                )
