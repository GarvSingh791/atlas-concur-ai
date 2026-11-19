"""Optional LLM-based narrative enrichment for the Travel Agent (P4-T9, FR-T6).

Generates plain-language 'why this option' summaries via the LLM provider.
Gracefully degrades to the existing templated explanations on ProviderError
(NFR-R1) — the shortlist is still returned, the caller marks status=needs_review
and records route=deterministic to signal the partial degradation.
"""

from __future__ import annotations

import dataclasses
import logging

from ai_agents.agents.travel_agent.ranking import RankedOption
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

_NARRATIVE_PROMPT = """\
You are a corporate travel advisor. In 2–3 sentences, explain why this travel \
option is or is not recommended and its key trade-offs. Be direct; no bullet points.

Option: {summary}
Policy status: {policy_status}
Rank: {rank} of {total}"""


def enrich_with_narratives(
    shortlist: list[RankedOption],
    *,
    provider: LlmProvider,
) -> tuple[list[RankedOption], bool]:
    """Add LLM-generated explanations to each option.

    Returns (enriched_shortlist, degraded).
    degraded=True means at least one narrative call failed and the templated
    fallback was used; the caller should set status=needs_review.
    """
    enriched: list[RankedOption] = []
    degraded = False
    total = len(shortlist)

    for rank, opt in enumerate(shortlist, start=1):
        policy_status = "In policy" if opt.in_policy else f"Out of policy: {opt.policy_reason}"
        prompt = _NARRATIVE_PROMPT.format(
            summary=_option_summary(opt),
            policy_status=policy_status,
            rank=rank,
            total=total,
        )
        try:
            response = provider.complete([LlmMessage(role="user", content=prompt)])
            narrative = response.text.strip() or opt.explanation
        except ProviderError as exc:
            logger.warning(
                "TravelAgent narrative: ProviderError(%s) for option %s; "
                "using templated fallback (NFR-R1).",
                exc.code,
                opt.option_id,
            )
            narrative = opt.explanation
            degraded = True

        # Only the explanation changes: dataclasses.replace keeps every other field —
        # the process-rule notices (PTA-02 / AIR-06 / AIR-04) and the AIR-06 baseline
        # were silently dropped by the old field-by-field copy (known limits, AIR-04).
        enriched.append(dataclasses.replace(opt, explanation=narrative))

    return enriched, degraded


def _option_summary(opt: RankedOption) -> str:
    if opt.segment_type == "flight":
        parts = [opt.supplier]
        if opt.cabin_class:
            parts.append(opt.cabin_class)
        if opt.is_direct:
            parts.append("direct")
        if opt.duration_hours:
            parts.append(f"{opt.duration_hours:.1f}h")
        parts.append(f"${opt.amount_usd:.0f}")
        return " / ".join(parts)
    # hotel
    parts = [opt.supplier]
    if opt.room_type:
        parts.append(opt.room_type)
    if opt.city:
        parts.append(f"in {opt.city}")
    rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
    parts.append(f"${rate:.0f}/night")
    return " / ".join(parts)
