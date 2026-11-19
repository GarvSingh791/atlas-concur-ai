"""Document-qualified, per-sentence citation grounding for policy answers (R5-T2).

Turns a free-text LLM answer that carries inline ``[T&E Policy §AIR-01]`` markers into a
machine-readable ``CitedAnswer`` — one ``CitedSentence`` per answer sentence, each carrying
the ``Citation`` objects whose ``ref`` the sentence actually cites.

Enforcement is STRICT at the sourcing layer: only refs that (a) appear as an inline marker
AND (b) match an available retrieval ``Citation`` become citations.  A marker for an unknown
ref is dropped (an LLM cannot fabricate a citation), and a sentence with no valid marker is
kept with ``citations=()`` and surfaced via ``CitedAnswer.needs_review`` — the caller
(``PolicyQaHypothesizer``) then refuses to assert an answer with zero verified citations.

Design (Fence 2 — layering):
  - Pure module: imports ONLY ``ai_agents.guardrails.citations``, the pure
    ``ai_agents.rag.support`` / ``lexical`` scorers + stdlib.  It
    does NOT import ``ai_agents.rag.retrieval`` — clauses are accepted via the structural
    ``ClauseLike`` Protocol, keeping the import graph acyclic.
  - ``extract_cited_sentences`` REUSES the retrieval ``Citation`` objects (matched by ``ref``)
    rather than rebuilding them, so the leaf-unit ``detail`` and the ``policy_name`` source are
    preserved exactly and no field is duplicated onto ``GroundedClause``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from ai_agents.guardrails.citations import Citation
from ai_agents.rag.support import (
    SentenceSupport,
    SupportContext,
    SupportInput,
    assess_support,
    carries_claim,
    concepts,
)

# Maps ``policies.document_type`` (policy|faq|howto|reference) to the short human label used
# in a citation prefix.  Keys MUST match the ``policy_document_type`` PG enum values exactly
# (database/models/policy.py).  An unknown / NULL document_type falls back to "Policy".
SHORT_PREFIX: dict[str, str] = {
    "policy": "T&E Policy",
    "faq": "T&E FAQ",
    "howto": "How-To",
    "reference": "Reference",
}


class ClauseLike(Protocol):
    """Structural view of a clause for labelling — satisfied by ``GroundedClause``.

    Declared here (not imported from ``retrieval``) to keep this module import-cycle-free.
    Read-only ``@property`` members so a frozen dataclass (``GroundedClause``) satisfies it.
    """

    @property
    def clause_ref(self) -> str | None: ...

    @property
    def document_type(self) -> str | None: ...


class ContextClauseLike(Protocol):
    """A clause as shown to the generator: its ref plus the (parent-section) text it was shown."""

    @property
    def clause_ref(self) -> str | None: ...

    @property
    def chunk_text(self) -> str: ...


# A section's units each start with their own id ("MIL-02 Business miles only: ...").
_UNIT_ID_RE = re.compile(r"^(?P<ref>[A-Z]{2,}-[A-Z0-9]+)\b")


def section_citations(
    clauses: tuple[ContextClauseLike, ...] | list[ContextClauseLike],
    citations: tuple[Citation, ...],
) -> tuple[Citation, ...]:
    """The citations a draft may verifiably use: the retrieved leaves PLUS the section siblings
    the generator was SHOWN.

    The generator sees each retrieved policy clause's whole enclosing section (R1 parent
    context: ``"\\n\\n"``-joined units, each opening with its own id), so it can — correctly —
    cite a sibling it read there ("MIL-02 Business miles only ..." while only MIL-01 was
    retrieved).  Without this the strict verifier drops that marker as unknown and the sentence
    reads as uncited.  Each sibling becomes a citation whose ``detail`` is exactly the unit text
    the model was shown (same source / document type as the retrieved clause), so the support
    gate checks the sentence against that unit like any other.  Retrieved citations keep their
    order and come first; nothing is added for a clause shown without parent expansion.
    Pure; never raises.
    """
    out = list(citations)
    known = {c.ref for c in citations if c.ref}
    for clause, citation in zip(clauses, citations, strict=False):
        if not clause.chunk_text or clause.chunk_text == citation.detail:
            continue
        for unit in clause.chunk_text.split("\n\n"):
            unit = unit.strip()
            m = _UNIT_ID_RE.match(unit)
            if m is None or m.group("ref") in known:
                continue
            known.add(m.group("ref"))
            out.append(
                Citation(
                    kind=citation.kind,
                    ref=m.group("ref"),
                    detail=unit,
                    source=citation.source,
                    document_type=citation.document_type,
                )
            )
    return tuple(out)


def citation_label(clause: ClauseLike) -> str:
    """Return the document-qualified inline marker for *clause*, e.g. ``[T&E Policy §AIR-01]``.

    Uses ``SHORT_PREFIX[document_type]`` for the document label, falling back to "Policy" when
    ``document_type`` is NULL/unknown.  Drops the ``§ref`` suffix for an unlabelled clause.
    """
    prefix = SHORT_PREFIX.get(clause.document_type or "", "Policy")
    if clause.clause_ref:
        return f"[{prefix} §{clause.clause_ref}]"
    return f"[{prefix}]"


@dataclass(frozen=True)
class CitedSentence:
    """One answer sentence with the citations it explicitly grounds itself on.

    ``citations=()`` means the sentence carried no valid inline marker — it is retained (never
    silently dropped) so the critic still sees the full answer, and it flags the parent
    ``CitedAnswer`` as ``needs_review``.
    """

    text: str
    citations: tuple[Citation, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain-JSON shape (safe for ``json.dumps`` on the streaming path)."""
        return {
            "text": self.text,
            "citations": [c.to_dict() for c in self.citations],
            "needs_review": not self.citations,
        }


@dataclass(frozen=True)
class CitedAnswer:
    """A policy answer decomposed into per-sentence, citation-verified units (R5-T2).

    Stored in ``GroundedResult.value``.  ``__str__`` returns ``full_text`` so the existing
    critic (which reads ``str(value)``) is unaffected; ``to_dict`` gives the machine-readable
    structure for the API / frontend chips (R6-T2).
    """

    sentences: tuple[CitedSentence, ...]

    @property
    def full_text(self) -> str:
        """The answer text (all sentences joined) — the human-facing string."""
        return " ".join(s.text for s in self.sentences)

    @property
    def needs_review(self) -> bool:
        """True when the answer is empty OR any sentence lacks a verified citation."""
        return (not self.sentences) or any(not s.citations for s in self.sentences)

    def distinct_citations(self) -> tuple[Citation, ...]:
        """All verified citations across sentences, de-duplicated by ``ref`` (order-preserving)."""
        seen: set[str] = set()
        out: list[Citation] = []
        for sentence in self.sentences:
            for citation in sentence.citations:
                if citation.ref not in seen:
                    seen.add(citation.ref)
                    out.append(citation)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain-JSON shape (safe for ``json.dumps`` on the streaming path)."""
        return {
            "full_text": self.full_text,
            "needs_review": self.needs_review,
            "sentences": [s.to_dict() for s in self.sentences],
        }

    def __str__(self) -> str:  # keeps PolicyQaCritic's ``str(value)`` contract intact
        return self.full_text


# One ``PREFIX §ref`` entry.  ``prefix`` allows the four SHORT_PREFIX labels (letters, digits,
# ``&``, ``/``, ``-``, spaces); ``ref`` is an uppercase unit id like AIR-01 / FAQ-Q01 / FAQ-FY26 /
# AIR-CABIN (2+ leading caps, then ``-`` then alnum).  ``§`` is U+00A7.
_ENTRY_NC = r"(?:[A-Za-z0-9&/\- ]+?\s*)?§\s*[A-Z]{2,}-[A-Z0-9]+"
# A marker: one bracket holding one entry ``[T&E Policy §AIR-01]`` or several separated by ``,``
# / ``;`` — ``[T&E FAQ §FAQ-Q03, T&E Policy §AIR-02]`` (models group citations this way; each
# ref is still verified against retrieval individually).  A later entry may omit the prefix.
_MARKER_RE = re.compile(
    r"\[\s*(?:[A-Za-z0-9&/\- ]+?)\s*§\s*[A-Z]{2,}-[A-Z0-9]+" rf"(?:\s*[,;]\s*{_ENTRY_NC})*\s*\]"
)
_MARKER_REF_RE = re.compile(r"§\s*(?P<ref>[A-Z]{2,}-[A-Z0-9]+)")

# Sentence boundary: end punctuation, then whitespace, then a plausible sentence start (capital,
# opening quote/paren/bracket, or a masked-marker sentinel), or end of string.  Applied to the
# MASKED text so a ``.`` inside a ``[... §ref]`` marker (or a numeric ``§4.2.1``) is never a split.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[\x00]|$)")
_UNMASK_RE = re.compile(r"\x00(\d+)\x00")
# The run of masked markers a split part opens with / a part that is nothing but markers.
_LEADING_MARKERS_RE = re.compile(r"^(?:\x00\d+\x00\s*)+")
_SENTINEL_RE = re.compile(r"\x00\d+\x00")


def _is_bare(part: str) -> bool:
    """A split part made of citation markers alone (no sentence text)."""
    return not _SENTINEL_RE.sub("", part).strip()


def _carries_claim(part: str) -> bool:
    """The part states something a clause could back — not a bare yes/no verdict ("Yes.",
    "Yes, you can.") or empty scaffolding ("Note this.")."""
    return carries_claim(_SENTINEL_RE.sub(" ", part))


# Discourse words a draft's opening INTRODUCES its answer with ("Intro.", "Here is a summary.",
# "The policy says the following.") — a part made of these alone states no claim.
_INTRO_CONCEPTS: frozenset[str] = frozenset(
    c
    for w in (
        "intro",
        "introduction",
        "overview",
        "summary",
        "answer",
        "following",
        "below",
        "says",
        "explains",
        "outlines",
    )
    for c in concepts(w)
)


def _trailing_style(parts: list[str]) -> bool:
    """Is the draft written in the trailing style (``"A. [§X] B. [§Y]"``)?  Decided ONCE.

    A trailing draft ends with a bare marker run after its final full stop.  That alone does
    not decide it — a leading-style draft can also close on one stray marker (``"Intro. [§X]
    A. [§Y] B. [§Z]"``), and reading it as trailing would shift every citation one sentence
    back (Intro<-X, A<-Y, B<-Z).  The two read the same by marker positions, so the OPENING
    decides: the text before the first marker run is a claim in a trailing draft (``"A."``
    cited by the run after it) and only a verdict / scaffold / intro in a leading one
    (``"Yes."``, ``"Note."``, ``"Intro."`` — nothing for the run after it to cite).
    """
    if len(parts) < 2 or not _is_bare(parts[-1]):
        return False
    opening = _SENTINEL_RE.split(parts[0], maxsplit=1)[0]
    words = set(concepts(opening))
    return _carries_claim(opening) and not (words and words <= _INTRO_CONCEPTS)


def _reattach_trailing_markers(parts: list[str]) -> list[str]:
    """Give citations written AFTER a sentence's full stop back to that sentence.

    Some drafts cite in a trailing style — ``"Every trip needs approval. [§PTA-01] Book via
    the tool. [§HOW-03]"`` — and a split on ``". "`` hands each marker to the NEXT sentence
    (the first is left uncited, the second cites the wrong clause, the last marker becomes a
    sentence of its own).  The style is read from the answer as a WHOLE (``_trailing_style``).
    In trailing style a marker run that opens a part is moved back to the previous part — only
    when that previous part cites nothing itself and states a claim: a bare verdict ("Yes.",
    "Yes, you can.") never gains a citation (no clause can back the question's premise), and
    the run that followed it is dropped rather than left to shift onto the wrong sentence.
    Otherwise a leading marker stays with the sentence it opens: ``"Intro. [§X] A. [§Y] B."``
    keeps A<-X and B<-Y (Intro uncited), ``"A [§X]. Note. [§Y] B."`` keeps A<-X, B<-Y, and in
    ``"Intro. [§X] A. [§Y] B. [§Z]"`` the stray final run goes to B.  A reattached citation is
    still checked against the sentence it now backs by the support gate, so a wrong reading
    cannot ground an unsupported sentence.
    """
    out = list(parts)
    if not _trailing_style(out):
        if len(out) >= 2 and _is_bare(out[-1]) and _carries_claim(out[-2]):
            out[-2] = f"{out[-2]} {out[-1].strip()}"
            out[-1] = ""
        return out
    for i in range(1, len(out)):
        lead = _LEADING_MARKERS_RE.match(out[i])
        if lead is None or _SENTINEL_RE.search(out[i - 1]):
            continue
        if _carries_claim(out[i - 1]):
            out[i - 1] = f"{out[i - 1]} {lead.group(0).strip()}"
        elif _is_bare(out[i]):
            continue  # after a verdict: a final bare run stays on its own
        # else: the run belonged to a verdict / scaffold part — dropped, not shifted onward
        out[i] = out[i][lead.end() :]
    return out


def extract_cited_sentences(
    raw_text: str,
    available_citations: tuple[Citation, ...],
) -> CitedAnswer:
    """Parse *raw_text* into a ``CitedAnswer``, verifying inline markers against retrieval.

    For each sentence, every ``[PREFIX §ref]`` marker whose ``ref`` matches a Citation in
    *available_citations* becomes a (reused) citation on that sentence; unknown refs are
    dropped.  Sentences are split robustly — markers (and their internal ``.``) are masked
    before splitting so a trailing ``[… §LOD-01].`` or a numeric ``§4.2.1`` never splits
    mid-citation.
    """
    by_ref: dict[str, Citation] = {c.ref: c for c in available_citations if c.ref}

    # Strip NUL bytes up front: NUL is our internal masking sentinel and has no legitimate place
    # in a policy answer, so a pre-existing ``\x00N\x00``-shaped substring must not survive to
    # collide with a real marker index at unmask time (would IndexError — this is a pure parser
    # that must NEVER raise, per reflect()'s contract).
    raw_text = raw_text.replace("\x00", "")

    # 1. Mask each marker with a punctuation-free sentinel so splitting can't break inside it.
    markers: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        markers.append(match.group(0))
        return f"\x00{len(markers) - 1}\x00"

    masked = _MARKER_RE.sub(_stash, raw_text.strip())

    # 2. Split into sentences on the masked text.
    parts = _SENTENCE_SPLIT_RE.split(masked) if masked else []
    parts = _reattach_trailing_markers(parts)

    # 3. Unmask, parse markers, verify against retrieval, build CitedSentences.
    sentences: list[CitedSentence] = []
    for part in parts:
        text = _UNMASK_RE.sub(lambda m: markers[int(m.group(1))], part).strip()
        if not text:
            continue
        cites: list[Citation] = []
        seen: set[str] = set()
        for marker in _MARKER_RE.finditer(text):
            for entry in _MARKER_REF_RE.finditer(marker.group(0)):
                ref = entry.group("ref")
                citation = by_ref.get(ref)
                if citation is not None and ref not in seen:
                    seen.add(ref)
                    cites.append(citation)
        sentences.append(CitedSentence(text=text, citations=tuple(cites)))

    return CitedAnswer(sentences=tuple(sentences))


# --------------------------------------------------------------------------------- R5-T3: NLI
# Deterministic entailment supplement to the LLM critic.  A cited sentence is "entailed" when the
# clause text it cites supports it.  Zero LLM/network cost, so it runs on the common answer path
# without touching the latency budget and stays deterministic under CI/eval.  It supplements —
# never replaces — the LLM critic: it catches an answer carrying a *valid* citation whose leaf text
# does not actually support the sentence's claim (a present-but-unsupporting citation).
#
# C7-06/C7-10 residual: the R5-T3 scorer was a raw token-set overlap (no stop words, no stemming)
# that scored faithful paraphrases 0.30-0.50 and withheld 8/14 clear in-corpus staging answers.
# The scorer now lives in ``ai_agents.rag.support``: content-concept recall (stop words removed,
# inflection/derivation folded, a closed T&E synonym map, question-premise echo neutral) plus
# HARD guards — every number/amount/percentage must appear in the cited evidence (or be a premise
# from the question), polarity must match the best-overlapping evidence segment, and a claim term
# borrowed from a DIFFERENT retrieved clause marks a mis-attribution.  The inline ``[Label §ref]``
# markers are still stripped before scoring (answer metadata, never in the chunk).
_DEFAULT_ENTAILMENT_THRESHOLD = 0.70
# Document types whose units restate policy clauses (and name them): see support._restates_cited.
_DERIVATIVE_TYPES = frozenset({"faq", "howto", "reference"})


@dataclass(frozen=True)
class EntailmentResult:
    """Per-answer verdict of the deterministic entailment check (R5-T3, strengthened C7).

    ``entailed`` is True only when EVERY sentence is supported; ``min_score`` is the weakest
    sentence's support score (the binding one); ``scores`` is per-sentence, in answer order.
    ``sentences`` carries the per-sentence verdicts (failed gates, missing numbers, unsupported
    terms) for sentence-level filtering and the internal audit record — it is deliberately NOT
    part of ``to_dict`` (the historical ``nli_result`` shape is kept stable).
    """

    entailed: bool
    min_score: float
    scores: tuple[float, ...]
    threshold: float
    sentences: tuple[SentenceSupport, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON shape for the single audit write (safe for ``json.dumps``)."""
        return {
            "entailed": self.entailed,
            "min_score": self.min_score,
            "scores": list(self.scores),
            "threshold": self.threshold,
        }


def check_entailment(
    cited: CitedAnswer,
    *,
    threshold: float = _DEFAULT_ENTAILMENT_THRESHOLD,
    question: str = "",
    retrieved: tuple[Citation, ...] = (),
    strict_verdicts: bool = False,
) -> EntailmentResult:
    """Check that every sentence of *cited* is supported by the leaf text it cites (deterministic).

    Per sentence (see ``ai_agents.rag.support``): content-concept recall against the
    concatenated ``detail`` of the citations the sentence carries, plus the numeric, polarity
    and contrast guards.  *question* supplies premise terms/numbers the answer may echo;
    *retrieved* (every retrieved citation) feeds the contrast guard.  An UNCITED sentence has
    no evidence and fails.  An empty answer (no sentences) is vacuously entailed
    (``min_score`` 1.0).  *strict_verdicts* (the calibrated path) fails a cited sentence that is
    only a yes/no verdict.  Pure function — never raises, no external calls.
    """
    items = tuple(
        SupportInput(
            text=sentence.text,
            cited_refs=tuple(c.ref for c in sentence.citations),
            evidence=tuple(c.detail for c in sentence.citations if c.detail),
            evidence_refs=tuple(c.ref for c in sentence.citations if c.detail),
        )
        for sentence in cited.sentences
    )
    cited_all = tuple(c for sentence in cited.sentences for c in sentence.citations)
    ctx = SupportContext(
        question=question,
        retrieved=tuple((c.ref, c.detail) for c in retrieved if c.ref and c.detail),
        derivative_refs=frozenset(
            c.ref
            for c in (*retrieved, *cited_all)
            if c.ref and c.document_type in _DERIVATIVE_TYPES
        ),
        strict_verdicts=strict_verdicts,
    )
    verdicts = assess_support(items, ctx, threshold=threshold)
    scores = tuple(v.score for v in verdicts)
    return EntailmentResult(
        entailed=all(v.supported for v in verdicts),
        min_score=min(scores) if scores else 1.0,
        scores=scores,
        threshold=threshold,
        sentences=verdicts,
    )
