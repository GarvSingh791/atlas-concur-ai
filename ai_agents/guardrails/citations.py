"""Grounding / citation contract (FR-A4).

Types:
    CitationKind — what kind of evidence backs the claim.
    Citation — one piece of evidence (clause ref, source field, or tool-result ref).
    GroundedResult — a claim/value pair with its supporting citations and confidence.

Validator:
    validate_grounding — enforces the contract: a confident inferred claim REQUIRES
    >=1 citation; without citations OR below the confidence threshold the result is
    returned as NOT asserted ("policy is silent" / needs-review outcome).  The
    validator NEVER raises and NEVER promotes an uncited claim to asserted.

Serialization:
    GroundedResult.to_dict() produces the list-of-citation-dicts shape expected by
    conversation_messages.citations (JSONB) and ai_decision_logs JSONB fields.
    Shape: {"asserted": bool, "confidence": float, "citations": [...]}
    Each citation dict: {"kind": str, "ref": str, ??"detail": str, ??"source": str}
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

DEFAULT_CITATION_THRESHOLD: float = 0.7
"""Minimum confidence required for a claim to be asserted (FR-A4)."""

LOCAL_COSINE_CITATION_THRESHOLD: float = 0.55
"""Citation floor on the cosine path of the LOCAL reranker (C7R; see resolve_citation_threshold).

Cosine confidence is ``0.5 + 0.5 * (top_sim - gate) / (1 - gate)`` (``retrieval._confidence``,
clamped to >= 0.5), so 0.55 == top similarity 0.28 at the 0.20 gate — a real bar, not the
gate's own image.  Calibrated at the AGENT level on the local path (real Gemini drafts, 27
answerable gold questions + all 52 unanswerable, decision logic swept over the same drafts):
with the question-relevance gate, 0/52 unanswerable are grounded at every floor in
[0.50, 0.70], and answerable coverage is maximal for any floor <= 0.559 (the lowest confidence
of a verified, on-topic answerable draft: "By when must I submit an expense report?"); 0.55 is
the highest round floor that keeps it.  (That live draft opened with an odd "Yes," and is now
held provisional by the relevance gate's yes/no-verdict rule; a plain answer at the same
confidence is grounded.)
WITHOUT the relevance gate only a floor >= 0.69 (i.e. the historical 0.70) grounds zero
unanswerable questions (vague ones such as "What's the cap?" at 0.688), which is why the
relevance gate is required below DEFAULT_CITATION_THRESHOLD.
"""


class CitationKind(str, enum.Enum):
    """Category of evidence backing a grounded claim."""

    clause_ref = "clause_ref"
    """A policy clause identifier (e.g. policy rule UUID or clause number)."""
    source_field = "source_field"
    """A structured data field (e.g. 'expense_report.amount')."""
    tool_result = "tool_result"
    """A tool-call result id (e.g. inventory search result reference)."""


@dataclass(frozen=True)
class Citation:
    """One piece of evidence backing a grounded claim.

    Serialized into conversation_messages.citations and ai_decision_logs JSONB.
    """

    kind: CitationKind
    ref: str
    """Clause id, field name, or tool-result id — the primary evidence pointer."""
    detail: str | None = None
    """Human-readable excerpt or description of the cited evidence."""
    source: str | None = None
    """Originating document or data source (e.g. policy name, table name)."""
    document_type: str | None = None
    """Parent document discriminator (policy|faq|howto|reference) — trust-surface hint.

    Additive + nullable; propagated from the retrieved chunk's parent document so the
    UI/audit trail can qualify the citation's source kind.  Serialized ONLY when set."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind.value, "ref": self.ref}
        if self.detail is not None:
            d["detail"] = self.detail
        if self.source is not None:
            d["source"] = self.source
        if self.document_type is not None:
            d["document_type"] = self.document_type
        return d


@dataclass(frozen=True)
class GroundedResult:
    """A claim/value pair with its supporting citations and confidence indicator.

    asserted=True  → the claim is backed by >=1 citation AND meets the confidence
                     threshold; safe to present to the user.
    asserted=False → policy is silent OR evidence is insufficient; the claim
                     should be surfaced as needing human review, not presented as
                     a confident answer.

    Serialize with .to_dict() for JSONB storage.
    """

    claim: str
    """Human-readable description of what is being claimed."""
    value: Any
    """The structured value backing the claim (policy limit, recommended option, etc.)."""
    citations: tuple[Citation, ...]
    confidence: float
    asserted: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the conversation_messages.citations / JSONB shape."""
        return {
            "asserted": self.asserted,
            "confidence": self.confidence,
            "citations": [c.to_dict() for c in self.citations],
        }


def validate_grounding(
    result: GroundedResult,
    *,
    threshold: float = DEFAULT_CITATION_THRESHOLD,
) -> GroundedResult:
    """Enforce the citation contract (FR-A4).

    Returns a GroundedResult with asserted=True only when:
      - confidence >= threshold, AND
      - at least one citation is present.

    Otherwise returns a copy with asserted=False.  Never raises.  Never
    promotes an uncited or low-confidence claim to asserted status.
    """
    if result.confidence >= threshold and len(result.citations) >= 1:
        if result.asserted:
            return result
        return GroundedResult(
            claim=result.claim,
            value=result.value,
            citations=result.citations,
            confidence=result.confidence,
            asserted=True,
        )
    return GroundedResult(
        claim=result.claim,
        value=result.value,
        citations=result.citations,
        confidence=result.confidence,
        asserted=False,
    )


def resolve_citation_threshold(
    confidence_source: str | None,
    tau_star: float | None,
    *,
    rerank_provider: str | None = None,
) -> float:
    """Scale-aware citation-contract confidence floor (FR-A4).

    The confidence signal changed scale between paths (R3): the CALIBRATED path reports the
    Platt reranker probability gated at the operating point tau* (~0.039); the COSINE path
    reports the [0.5, 1.0] rescale of the top similarity above the cosine gate.

    * calibrated (tau* applied) -> tau*.  A fixed 0.70 is unsatisfiable on that scale.
    * cosine on the LOCAL reranker (local Docker + staging: no calibrated gate exists by
      design, ``abstention_threshold.json`` "local" is not promoted) ->
      LOCAL_COSINE_CITATION_THRESHOLD.  The local embedder's in-corpus top similarities give
      confidences of ~0.55-0.70, so the fixed 0.70 withheld most real answers.  This floor is
      NOT sufficient on its own: below DEFAULT_CITATION_THRESHOLD the PolicyAgent also requires
      the answer's cited clauses to cover the question (``support.question_relevance``) before
      it may be labelled grounded — otherwise it stays a provisional needs_review answer.
    * anything else -> DEFAULT_CITATION_THRESHOLD (0.70, fail closed).  That includes a
      calibrated provider (novita / huggingface / cohere) that fell back to cosine because the
      reranker is down or tau* is missing: a degraded deployment keeps the historical bar.
      ``rerank_provider`` omitted -> the same fail-closed 0.70.

    Mirrors the R6-T2 confidence_source split.
    """
    if confidence_source == "calibrated" and tau_star is not None:
        return tau_star
    if confidence_source == "cosine" and rerank_provider == "local":
        return LOCAL_COSINE_CITATION_THRESHOLD
    return DEFAULT_CITATION_THRESHOLD
