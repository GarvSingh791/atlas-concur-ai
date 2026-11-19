"""Policy Q&A hypothesizer + critic for the reflection loop (Cap 2, P4-T8).

PolicyQaHypothesizer — seeds a policy answer from the retrieved clauses via LLM.
PolicyQaCritic       — checks that the answer follows only from the cited clauses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agents.guardrails.citations import Citation, GroundedResult
from ai_agents.guardrails.reflection import Critique
from ai_agents.rag.grounding import citation_label, extract_cited_sentences
from ai_agents.rag.retrieval import GroundedClause
from config.settings import get_settings
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

# Package B prompt-cache split (wired, inert at ~150 tokens < Gemini's ~1024 cache floor,
# not live-verified). The static persona + citation/abstention rules never vary per request,
# so on the LIVE path we hand them to the provider as a separate cache=True SYSTEM message and
# keep the per-request clauses/question in a following USER message (see seed()). At the current
# static-block size the cache is INERT/forward-looking. NOTE: on the live path this splits ONE
# fused user message into system+user — a GENERATION-OUTPUT change that must be re-validated on
# the deferred live run (citation completeness + nli entailment rate). The LOCAL path is left
# byte-identical by reusing _SEED_PROMPT_V2 (== _STATIC_INSTRUCTIONS + "\n\n" + _QUERY_TEMPLATE),
# so eval-db stays byte-identical.
_STATIC_INSTRUCTIONS = """\
You are a travel-and-expense policy assistant. Answer the following policy question
based ONLY on the provided policy clauses. Do not use any other knowledge.

Give a yes/no/conditional answer in 1-3 sentences. For EVERY sentence in your answer,
end it with the citation label(s) that support it, copied EXACTLY from the clause list
below, in square brackets — for example [T&E Policy §AIR-01]. Never write a sentence
without a citation. If no clause supports a claim, omit the claim."""

_QUERY_TEMPLATE = """\
Policy clauses:
{clauses}

Question: {question}

Answer (1-3 sentences, each ending with its supporting [Label §ref] citation):"""

# Fused single-message form for the LOCAL deterministic path — BYTE-IDENTICAL to the
# pre-split prompt so LLM_PROVIDER==local (CI/eval-db) output does not move.
_SEED_PROMPT_V2 = _STATIC_INSTRUCTIONS + "\n\n" + _QUERY_TEMPLATE

_CRITIC_PROMPT = """\
You are a fact-checker. Determine whether the answer below is fully supported by
the provided policy clauses. The answer must NOT assert anything not in the clauses.

Policy clauses:
{clauses}

Answer to check: {answer}

Reply with one word only: SUPPORTED, CONTRADICTED, or UNSUPPORTED.
Then on the next line give a short rationale (1 sentence)."""


# C7R: the critic's first non-empty line carries the verdict.  Models decorate it
# ("**SUPPORTED**", "Verdict: SUPPORTED", '"SUPPORTED."', a leading blank line);
# a plain startswith() read all of those as inconclusive and withheld a supported
# answer at random.  Leading punctuation / markdown and an optional "Verdict:"
# label are skipped; the verdict must be a whole word, so "UNSUPPORTED" and
# "NOT SUPPORTED" are never read as SUPPORTED.
_VERDICT_RE = re.compile(
    r"^[\W_]*(?:verdict[\W_]*)?(SUPPORTED|UNSUPPORTED|CONTRADICTED)\b[\W_]*(.*)$",
    re.IGNORECASE,
)


def parse_critic_verdict(text: str) -> tuple[str | None, str]:
    """Return ``(verdict, rationale)`` from a critic reply (C7R).

    ``verdict`` is "SUPPORTED" / "UNSUPPORTED" / "CONTRADICTED", or None when the
    first non-empty line does not START with one (fail closed: the caller treats
    None as inconclusive).  ``rationale`` is the rest of that line, else the next
    non-empty line, else the whole reply.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None, ""
    match = _VERDICT_RE.match(lines[0])
    if match is None:
        return None, lines[1] if len(lines) > 1 else lines[0]
    rest = match.group(2).strip()
    rationale = rest or (lines[1] if len(lines) > 1 else lines[0])
    return match.group(1).upper(), rationale


def _complete_deterministic(provider: LlmProvider, prompt: str) -> str:
    """One critic completion at POLICY_QA_CRITIC_TEMPERATURE (default 0.0) (C7R).

    A provider that cannot set the temperature (or a duck-typed test double that
    is not an ``LlmProvider``) is called with plain ``complete``.
    """
    messages = [LlmMessage(role="user", content=prompt)]
    if isinstance(provider, LlmProvider):
        temperature = get_settings().POLICY_QA_CRITIC_TEMPERATURE
        return provider.complete_with_options(messages, temperature=temperature).text
    return provider.complete(messages).text


@dataclass
class PolicyQaHypothesizer:
    """Seeds and revises a policy Q&A answer constrained to retrieved clauses."""

    provider: LlmProvider
    clauses: Sequence[GroundedClause]
    question: str
    citations: tuple[Citation, ...]
    confidence: float

    def _clause_text(self) -> str:
        # R5-T2: show the LLM the EXACT document-qualified label to copy per sentence.
        return "\n".join(f"- {citation_label(c)} {c.chunk_text}" for c in self.clauses)

    def _build_seed_messages(self) -> list[LlmMessage]:
        # LOCAL: one fused user message, BYTE-IDENTICAL to today (eval-db stability).
        # LIVE: split into a cache=True system block + a per-request user block so the
        # static persona/rules become a cacheable prefix (Package B; inert at current size).
        clauses = self._clause_text()
        if get_settings().LLM_PROVIDER == "local":
            return [
                LlmMessage(
                    role="user",
                    content=_SEED_PROMPT_V2.format(clauses=clauses, question=self.question),
                )
            ]
        return [
            LlmMessage(role="system", content=_STATIC_INSTRUCTIONS, cache=True),
            LlmMessage(
                role="user",
                content=_QUERY_TEMPLATE.format(clauses=clauses, question=self.question),
            ),
        ]

    def seed(self) -> GroundedResult:
        try:
            response = self.provider.complete(self._build_seed_messages())
            answer = response.text.strip()
        except ProviderError as exc:
            logger.warning("PolicyQaHypothesizer: LLM failed (%s); returning empty claim", exc.code)
            answer = ""

        if not answer:
            # Empty answer: keep a FALSY value so PolicyQaCritic's empty short-circuit fires
            # (a CitedAnswer instance is always truthy and would defeat it).
            return GroundedResult(
                claim=self.question,
                value="",
                citations=(),
                confidence=self.confidence,
                asserted=False,
            )

        # R5-T2 STRICT: citations come ONLY from the LLM's verified inline [Label §ref] markers
        # (matched against the retrieval citations by ref).  A fully-uncited answer yields no
        # citations, so validate_grounding in reflect() refuses to assert it (→ needs_review).
        cited = extract_cited_sentences(answer, self.citations)
        return GroundedResult(
            claim=self.question,
            value=cited,
            citations=cited.distinct_citations(),
            confidence=self.confidence,
            asserted=False,  # validate_grounding in reflect() will promote
        )

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult:
        # Use the correction if provided, otherwise return previous as-is (will be rejected)
        if critique.correction is not None:
            return critique.correction
        return GroundedResult(
            claim=previous.claim,
            value=previous.value,
            citations=previous.citations,
            confidence=previous.confidence,
            asserted=False,
        )


@dataclass
class PolicyQaCritic:
    """Checks that a Q&A answer is fully supported by the retrieved clauses."""

    provider: LlmProvider
    clauses: Sequence[GroundedClause]

    def _clause_text(self) -> str:
        return "\n".join(f"- [{c.clause_ref or 'unlabelled'}] {c.chunk_text}" for c in self.clauses)

    def __call__(self, hypothesis: GroundedResult) -> Critique:
        answer = str(hypothesis.value) if hypothesis.value else ""
        if not answer:
            return Critique(
                verdict="unsupported",
                rationale="Empty answer cannot be verified.",
                needs_review=True,
            )

        prompt = _CRITIC_PROMPT.format(clauses=self._clause_text(), answer=answer)
        try:
            reply = _complete_deterministic(self.provider, prompt)
        except ProviderError as exc:
            logger.warning("PolicyQaCritic: LLM failed (%s); degrading to unsupported", exc.code)
            return Critique(
                verdict="unsupported",
                rationale=f"LLM critic unavailable: {exc.code}",
                needs_review=True,
            )

        verdict_raw, rationale = parse_critic_verdict(reply)

        if verdict_raw == "SUPPORTED":
            # Re-attach citations to the supported hypothesis for validate_grounding
            enriched = GroundedResult(
                claim=hypothesis.claim,
                value=hypothesis.value,
                citations=hypothesis.citations,
                confidence=hypothesis.confidence,
                asserted=False,
            )
            return Critique(verdict="supported", rationale=rationale, correction=enriched)
        if verdict_raw == "CONTRADICTED":
            return Critique(verdict="contradicted", rationale=rationale)
        return Critique(
            verdict="unsupported",
            rationale=rationale or "LLM critic inconclusive.",
            needs_review=True,
        )
