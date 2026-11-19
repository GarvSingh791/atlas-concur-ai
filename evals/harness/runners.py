"""Builders that turn dataset rows into the production input shapes.

These helpers are the single place that translates a JSONL dataset record into
the dataclasses the production code consumes, so the Tier-1 (hermetic) and
Tier-2 (DB) entrypoints stay in lockstep and never diverge in how they construct
``IngestedData`` / ``EvalItem`` / in-memory ``PolicyRule`` rows.

Nothing here calls a DB.  The ``PolicyRule`` rows built by ``build_policy_rules``
are *unsaved* ORM instances suitable for patching ``_load_active_rules`` in the
hermetic tier (no Session required).

WHY a duck-typed segment (``SimpleNamespace``) instead of ``ItinerarySegment``:
``smart_match`` only reads attribute access (``.segment_id``, ``.amount``,
``.start_datetime`` …) — never isinstance — so a tiny namespace object is the
minimal faithful stand-in and avoids importing the heavier itinerary tool just
to build a segment.  The attribute set mirrors BUILD BRIEF section 2.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from ai_agents.agents.expense_agent.ingest import CardTxnFact, IngestedData, ReceiptFact
from ai_agents.agents.expense_agent.matching import MatchCandidate
from ai_agents.context import AgentContext
from ai_agents.tools.itinerary_calendar import (
    CalendarEntry,
    ItineraryIntent,
    ItineraryResult,
    ItinerarySegment,
)
from ai_agents.tools.policy_eval import EvalItem
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from evals.harness.loaders import MA1Case, PolicyRuleDef
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.interface import PresignedUrl, StorageProvider

__all__ = [
    "build_segment",
    "build_ingested_data",
    "build_eval_item",
    "build_policy_rules",
    "produced_linkages",
    "gold_linkages",
    "linkage_tuple",
    # M-A1 (Tier-2 DB) harness-owned stub providers
    "EvalOcr",
    "EvalJsonLlm",
    "EvalItinerary",
    "StorageStub",
    "build_itinerary_result",
]

# A linkage tuple is the canonical, comparable form of one MatchCandidate /
# one gold match: (receipt_id, card_txn_id, segment_id, is_duplicate).
LinkageTuple = tuple[str | None, str | None, str | None, bool]


def build_segment(s: dict[str, Any]) -> SimpleNamespace:
    """Duck-typed itinerary segment exposing the attributes ``smart_match`` reads."""
    return SimpleNamespace(
        segment_id=s["segment_id"],
        trip_id=s["trip_id"],
        segment_type=s["segment_type"],
        amount=s.get("amount"),
        currency=s.get("currency"),
        start_datetime=s.get("start_datetime"),
        end_datetime=s.get("end_datetime"),
        location=s.get("location"),
        supplier=s.get("supplier"),
        details=s.get("details"),
    )


def build_ingested_data(ingest: dict[str, Any]) -> IngestedData:
    """Build a production ``IngestedData`` from an m_a2 case ``ingest`` payload."""
    receipts = [
        ReceiptFact(
            receipt_id=str(r["receipt_id"]),
            amount=float(r["amount"]),
            currency=str(r["currency"]),
            date=str(r["date"]),
            vendor=r.get("vendor"),
            location=r.get("location"),
            expense_type=r.get("expense_type"),
            line_items=r.get("line_items"),
            already_linked=bool(r.get("already_linked", False)),
        )
        for r in ingest["receipts"]
    ]
    card_txns = [
        CardTxnFact(
            txn_id=str(t["txn_id"]),
            amount=float(t["amount"]),
            currency=str(t["currency"]),
            merchant=t.get("merchant"),
            txn_date=str(t["txn_date"]),
            last_four=t.get("last_four"),
            already_matched=bool(t.get("already_matched", False)),
        )
        for t in ingest["card_txns"]
    ]
    segments = [build_segment(s) for s in ingest["segments"]]
    return IngestedData(
        receipts=receipts,
        card_txns=card_txns,
        segments=segments,
        trip_id=ingest.get("trip_id"),
        unprocessed_receipts=list(ingest.get("unprocessed_receipts", [])),
    )


def build_eval_item(item: dict[str, Any]) -> EvalItem:
    """Build a production ``EvalItem``, filtering to declared dataclass fields.

    Mirrors PolicyEvalHandler.handle's payload-parse so the eval feeds the tool
    exactly the same way the production handler does (unknown keys ignored).
    """
    fields = EvalItem.__dataclass_fields__
    return EvalItem(**{k: v for k, v in item.items() if k in fields})


def build_policy_rules(defs: list[PolicyRuleDef]) -> list[PolicyRule]:
    """Build unsaved, active in-memory ``PolicyRule`` rows from rule defs.

    Used to patch ``PolicyEvalTool._load_active_rules`` in the hermetic tier so
    the REAL per-rule evaluation logic runs with no DB.  All rows share one
    sentinel ``policy_id`` (never persisted) and carry ``status=active`` so they
    survive the tool's active-only filter.
    """
    sentinel_policy_id = uuid.UUID(int=0)
    rules: list[PolicyRule] = []
    for d in defs:
        rules.append(
            PolicyRule(
                id=uuid.uuid4(),
                policy_id=sentinel_policy_id,
                rule_type=PolicyRuleType(d.rule_type),
                applies_to=PolicyAppliesTo(d.applies_to),
                definition=d.definition,
                source_clause_ref=d.source_clause_ref,
                status=PolicyRuleStatus.active,
            )
        )
    return rules


def linkage_tuple(candidate: MatchCandidate) -> LinkageTuple:
    """Canonical comparable linkage tuple for one produced MatchCandidate.

    ``MatchCandidate.segment`` is typed ``object | None`` (the production code
    treats segments duck-typed), so the segment id is read via ``getattr``.
    """
    segment_id = None
    if candidate.segment is not None:
        segment_id = getattr(candidate.segment, "segment_id", None)
    return (
        candidate.receipt.receipt_id if candidate.receipt else None,
        candidate.card_txn.txn_id if candidate.card_txn else None,
        segment_id,
        bool(candidate.is_duplicate_resolved),
    )


def produced_linkages(candidates: list[MatchCandidate]) -> set[LinkageTuple]:
    """Set of canonical linkage tuples produced by ``smart_match``."""
    return {linkage_tuple(c) for c in candidates}


def gold_linkages(gold: dict[str, Any]) -> set[LinkageTuple]:
    """Set of canonical linkage tuples from an m_a2 case's ``gold.matches``."""
    out: set[LinkageTuple] = set()
    for m in gold["matches"]:
        out.add(
            (
                m.get("receipt_id"),
                m.get("card_txn_id"),
                m.get("segment_id"),
                bool(m.get("is_duplicate", False)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# M-A1 (Tier-2 DB) — harness-owned deterministic stub providers.
#
# These live in ``evals/`` (NOT edits to ``services/``).  They exist because the
# production LOCAL providers are unfit for a labeled extraction eval:
#   * ``LocalLlmProvider.complete`` returns NON-JSON, so the real gap-fill parser
#     ([gap_fill.py:_parse_llm_response]) discards every inferred field — gap-fill
#     would always be empty.  ``EvalJsonLlm`` returns the case's ``gap_fill_json``
#     as VALID JSON so the REAL parser/router/reflection run end-to-end.
#   * ``LocalOcrProvider`` is canned for only 3 seeded image hints, so it cannot
#     drive 85 diverse cases.  ``EvalOcr`` returns the case's authored OCR payload
#     (fields + per-field confidence), which is what the routing threshold and
#     provenance logic consume.
# The pipeline code under test (routing @0.70, provenance, reflection, the receipt
# UPDATE, the decision-log write) is REAL and unmodified — only the leaf providers
# are stubbed, exactly as the established receipt-pipeline tests do
# [ai_agents/receipt_pipeline/tests/test_receipt_pipeline.py].
# ---------------------------------------------------------------------------

# A small, non-empty bytes blob returned for any storage key.  The pipeline
# routes these bytes to ``OcrProvider.extract`` (which our EvalOcr ignores) and
# must NEVER leak them into an LLM prompt (NFR-SEC4); using a recognisable
# marker lets a future safety probe assert that.
_STUB_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nATLAS_EVAL_STUB_IMAGE"


class EvalOcr(OcrProvider):
    """Deterministic OCR provider that replays one M-A1 case's OCR payload.

    ``extract`` ignores the image bytes entirely (the gold OCR output is authored
    per-case) and returns the case's ``OcrResult``.  This is the seam that feeds
    the real routing/provenance logic the exact ``(fields, field_confidence)`` the
    dataset specifies, so a confidence below 0.70 deterministically routes the
    field to gap-fill just as it would in production.
    """

    def __init__(self, case: MA1Case) -> None:
        ocr = case.ocr
        # Copy so a downstream mutation cannot bleed across cases.
        self._result = OcrResult(
            fields=dict(ocr["fields"]),
            field_confidence=dict(ocr["field_confidence"]),
        )
        self.calls: list[bytes | str] = []

    def extract(self, image: bytes | str) -> OcrResult:
        self.calls.append(image)
        return self._result


class EvalJsonLlm(LlmProvider):
    """LLM stub returning a case's ``gap_fill_json`` as VALID JSON.

    The production gap-fill prompt asks for a JSON object mapping each gap field
    to its inferred value; the real parser keeps only keys in ``gap_fields``.  We
    serialise the case's authored ``gap_fill_json`` (possibly ``{}`` when the
    case has no gap-fill) so the REAL parser/confidence-assignment/reflection
    path runs unchanged.  ``None`` values survive as JSON ``null`` — that is how
    an "unresolvable" gap field (e.g. a location the model cannot infer) is
    expressed, exercising the reflection-reject path.
    """

    def __init__(self, case: MA1Case) -> None:
        self._payload: dict[str, Any] = dict(case.gap_fill_json or {})
        self.prompts: list[str] = []

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        for m in messages:
            self.prompts.append(m.content)
        return LlmResponse(
            text=json.dumps(self._payload),
            model="eval-json-stub",
            usage=LlmUsage(input_tokens=0, output_tokens=0),
            tool_calls=[],
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield LlmChunk(delta=json.dumps(self._payload))


def build_itinerary_result(itinerary: dict[str, Any] | None) -> ItineraryResult:
    """Build a production ``ItineraryResult`` from an m_a1 case's ``itinerary`` dict.

    Returns an empty result when the case carries no itinerary.  The segment /
    calendar dataclasses are the REAL production types so the gap-fill window
    filter and the location-reflection critic consume them unchanged.
    """
    if not itinerary:
        return ItineraryResult(trips=(), segments=(), calendar_events=())
    segments = tuple(
        ItinerarySegment(
            segment_id=str(s["segment_id"]),
            trip_id=str(s["trip_id"]),
            segment_type=str(s["segment_type"]),
            supplier=s.get("supplier"),
            location=s.get("location"),
            start_datetime=s.get("start_datetime"),
            end_datetime=s.get("end_datetime"),
            amount=s.get("amount"),
            currency=s.get("currency"),
            details=s.get("details") or {},
        )
        for s in itinerary.get("segments", [])
    )
    events = tuple(
        CalendarEntry(
            event_id=str(e["event_id"]),
            title=e.get("title"),
            location=e.get("location"),
            start_datetime=str(e["start_datetime"]),
            end_datetime=e.get("end_datetime"),
        )
        for e in itinerary.get("calendar_events", [])
    )
    return ItineraryResult(trips=(), segments=segments, calendar_events=events)


class EvalItinerary:
    """Itinerary tool stub exposing ``get_itinerary`` from a case's itinerary dict.

    Duck-typed to ``ItineraryCalendarTool`` (the pipeline only ever calls
    ``.get_itinerary(intent, *, context, session)``).  Returns the case's
    pre-built ``ItineraryResult`` regardless of intent/context/session so the
    gap-fill grounding + reflection run on the authored facts — never the DB.
    """

    def __init__(self, case: MA1Case) -> None:
        self._result = build_itinerary_result(case.itinerary)
        self.calls: list[ItineraryIntent] = []

    def get_itinerary(
        self,
        intent: ItineraryIntent,
        *,
        context: AgentContext,
        session: object = None,
    ) -> ItineraryResult:
        self.calls.append(intent)
        return self._result


class StorageStub(StorageProvider):
    """Storage provider returning a fixed bytes blob for any key (no real I/O).

    The receipt pipeline resolves ``receipt.image_url`` to bytes via
    ``StorageProvider.get_object`` before handing them to OCR.  EvalOcr ignores
    the bytes, but the contract still requires a provider that yields some; this
    returns a recognisable marker blob.  The mutating methods are inert.
    """

    def __init__(self, data: bytes = _STUB_IMAGE_BYTES) -> None:
        self._data = data
        self.keys_read: list[str] = []

    def get_object(self, key: str) -> bytes:
        self.keys_read.append(key)
        return self._data

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        return None

    def delete_object(self, key: str) -> None:
        return None

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"stub://{key}", expires_in=expires_in)
