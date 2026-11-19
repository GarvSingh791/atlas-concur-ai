"""Decision-log writer — appends one ai_decision_logs row per AI invocation.

Mirrors backend/app/core/audit.py::record_audit semantics:
  - Accepts an injected SQLAlchemy Session (caller owns open + commit).
  - session.flush() surfaces DB/enum errors immediately and populates id/created_at.
  - Does NOT commit — the caller atomically commits the decision log together with
    any business mutation it accompanies.

Validation performed before persistence:
  - confidence range-checked to [0.000, 1.000] and quantized to 3 decimal places
    (matches NUMERIC(4,3) column).  Out-of-range → GuardrailError.
  - agent / route / reflection_outcome are typed as the real enum members; invalid
    values will be caught either at construction time (type checking) or on flush
    (DB enum constraint).
  - PII/secret minimizer runs on input_summary, tools_used, and output_summary
    before any data reaches the session (NFR-SEC5).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.decision_log.minimizer import minimize
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.exceptions import GuardrailError
from database.models import AiDecisionLog

logger = logging.getLogger(__name__)

_CONFIDENCE_QUANTIZE = Decimal("0.001")
_CONFIDENCE_MIN = Decimal("0.000")
_CONFIDENCE_MAX = Decimal("1.000")


def write_decision_log(
    session: Session,
    *,
    context: AgentContext,
    record: DecisionRecord,
) -> AiDecisionLog:
    """Append one row to ai_decision_logs within the caller's transaction.

    Args:
        session: The active SQLAlchemy session for the surrounding unit of work.
            The caller owns commit(); this function only flushes.
        context: Injected AgentContext carrying organization_id, user_id, and
            correlation_id for the log row.
        record: Structured description of the AI invocation.

    Returns:
        The flushed (not committed) AiDecisionLog row with id and created_at
        populated by the database server defaults.

    Raises:
        GuardrailError: confidence is outside [0.000, 1.000] or not numeric.
        sqlalchemy.exc.SQLAlchemyError: propagated from flush() on constraint
            violations (invalid enum value, NULL constraint, FK violation).
    """
    confidence_decimal: Decimal | None = None
    if record.confidence is not None:
        try:
            confidence_decimal = Decimal(str(record.confidence)).quantize(
                _CONFIDENCE_QUANTIZE, rounding=ROUND_DOWN
            )
        except InvalidOperation as exc:
            raise GuardrailError("confidence is not a valid number") from exc
        if not (_CONFIDENCE_MIN <= confidence_decimal <= _CONFIDENCE_MAX):
            raise GuardrailError(f"confidence must be in [0.000, 1.000]; got {confidence_decimal}")

    # PII/secret minimization on all three JSONB summary fields (NFR-SEC5).
    # Applied to tools_used as well — tool call inputs/outputs can carry PII.
    input_summary = minimize(record.input_summary) if record.input_summary is not None else None
    tools_used = minimize(record.tools_used) if record.tools_used is not None else None
    output_summary = minimize(record.output_summary) if record.output_summary is not None else None

    entry = AiDecisionLog(
        organization_id=context.organization_id,
        user_id=context.user_id,
        agent=record.agent,
        task_type=record.task_type,
        route=record.route,
        model_provider=record.model_provider,
        correlation_id=context.correlation_id,
        input_summary=input_summary,
        tools_used=tools_used,
        output_summary=output_summary,
        confidence=confidence_decimal,
        reflection_outcome=record.reflection_outcome,
        latency_ms=record.latency_ms,
    )
    # SAVEPOINT: a failed insert (e.g. an FK violation for an unknown org/user)
    # rolls back only this row, so callers that catch the error and "continue"
    # keep a usable transaction instead of a PendingRollbackError on their next
    # statement.  The exception still propagates (contract unchanged).
    with session.begin_nested():
        session.add(entry)
        session.flush()  # surface DB errors now; populate id/created_at; caller commits

    logger.info(
        "ai_decision agent=%s task=%s route=%s org=%s user=%s corr=%s",
        record.agent.value,
        record.task_type,
        record.route.value,
        context.organization_id,
        context.user_id if context.user_id is not None else "system",
        context.correlation_id or "",
    )
    return entry


@contextmanager
def latency_timer() -> Generator[list[int], None, None]:
    """Context manager that measures elapsed wall-clock time in milliseconds.

    Yields a 1-element list; after the block exits, element 0 holds the elapsed
    time in milliseconds.  Assign to DecisionRecord.latency_ms.

    Usage::

        with latency_timer() as ms:
            result = do_work()
        record = DecisionRecord(..., latency_ms=ms[0])
    """
    bucket: list[int] = [0]
    start = time.perf_counter()
    yield bucket
    bucket[0] = round((time.perf_counter() - start) * 1000)
