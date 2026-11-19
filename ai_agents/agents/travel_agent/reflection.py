"""Travel-specific reflection Hypothesizer + Critic (P4-T9, FR-A3).

TravelShortlistHypothesizer — wraps the ranked shortlist as a GroundedResult seed.
TravelPolicyHonestyCritic  — deterministic critic: checks that every option's
    display label (in_policy) is consistent with the ground-truth (rule_in_policy)
    from the PolicyVerdict.  If any option is silently mislabeled as in-policy,
    the critic corrects the label and demotes the option before output.

This is entirely LLM-free: the critic runs without any provider call, so the
reflection loop stays on the deterministic route and never degrades due to LLM
unavailability.
"""

from __future__ import annotations

import dataclasses

from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.guardrails.citations import Citation, CitationKind, GroundedResult
from ai_agents.guardrails.reflection import Critique


class TravelShortlistHypothesizer:
    """Seeds the reflection loop with the ranked shortlist."""

    def __init__(self, shortlist: list[RankedOption]) -> None:
        self._shortlist = shortlist

    def seed(self) -> GroundedResult:
        citations = _build_citations(self._shortlist)
        return GroundedResult(
            claim=(
                f"Ranked shortlist of {len(self._shortlist)} travel option(s) "
                "with correct policy labels"
            ),
            value=self._shortlist,
            citations=citations,
            confidence=0.9,
            asserted=False,  # critic must validate before asserting
        )

    def regenerate(self, previous: GroundedResult, critique: Critique) -> GroundedResult:
        # The critic always provides a correction when contradicted; use it.
        if critique.correction is not None:
            return critique.correction
        return previous


class TravelPolicyHonestyCritic:
    """Deterministic policy-honesty critic (no LLM).

    Verifies that every option's in_policy display label matches the ground-truth
    rule_in_policy value from the PolicyVerdict.  Any mismatch means the ranking
    step silently placed an out-of-policy option in an in-policy slot — a
    guardrail violation (FR-A3).

    On contradiction: rebuilds the shortlist with corrected labels and re-sorts
    so out-of-policy options are demoted; returns a correction GroundedResult.
    On no mismatch: returns supported with a high-confidence correction so
    validate_grounding can promote asserted=True.
    """

    def __call__(self, hypothesis: GroundedResult) -> Critique:
        shortlist = hypothesis.value
        if not isinstance(shortlist, list):
            return Critique(
                verdict="unsupported",
                rationale="Hypothesis value is not a list; cannot validate.",
                needs_review=True,
            )

        mismatches = [opt for opt in shortlist if opt.in_policy != opt.rule_in_policy]

        if not mismatches:
            # All labels correct — return supported with a validated GroundedResult
            return Critique(
                verdict="supported",
                rationale="All option policy labels are consistent with rule verdicts.",
                correction=GroundedResult(
                    claim=hypothesis.claim,
                    value=shortlist,
                    citations=hypothesis.citations,
                    confidence=0.95,
                    asserted=False,  # validate_grounding promotes this
                ),
            )

        # Mislabeled options found — correct labels and re-rank.
        corrected: list[RankedOption] = []
        for opt in shortlist:
            if opt.in_policy != opt.rule_in_policy:
                penalty = -200.0 if not opt.rule_in_policy else 0.0
                corrected.append(
                    dataclasses.replace(
                        opt,
                        in_policy=opt.rule_in_policy,  # corrected
                        policy_reason=(
                            opt.policy_reason + " [label corrected by reflection critic]"
                        ),
                        rank_score=opt.rank_score + penalty,
                        explanation=(
                            opt.explanation + " (Policy label corrected.)"
                            if not opt.rule_in_policy
                            else opt.explanation
                        ),
                    )
                )
            else:
                corrected.append(opt)

        corrected_sorted = sorted(corrected, key=lambda o: o.rank_score, reverse=True)
        corrected_citations = _build_citations(corrected_sorted)

        mismatch_ids = [opt.option_id for opt in mismatches]
        return Critique(
            verdict="contradicted",
            rationale=(
                f"Found {len(mismatches)} option(s) with inconsistent policy labels "
                f"({mismatch_ids}); labels corrected to match rule verdicts."
            ),
            correction=GroundedResult(
                claim=hypothesis.claim,
                value=corrected_sorted,
                citations=corrected_citations,
                confidence=0.95,
                asserted=False,
            ),
        )


# ---------------------------------------------------------------------------
# Citation builder
# ---------------------------------------------------------------------------


def _build_citations(shortlist: list[RankedOption]) -> tuple[Citation, ...]:
    """Build citations for the shortlist (tool_result per in-policy option + clause_refs)."""
    cits: list[Citation] = []
    seen_option_ids: set[str] = set()
    seen_clause_refs: set[str] = set()

    for opt in shortlist:
        # Cite every option regardless of policy status (the agent must ground all options)
        if opt.option_id not in seen_option_ids:
            cits.append(
                Citation(
                    kind=CitationKind.tool_result,
                    ref=opt.option_id,
                    source="mock_inventory",
                )
            )
            seen_option_ids.add(opt.option_id)
        for ref in opt.clause_refs:
            if ref not in seen_clause_refs:
                cits.append(
                    Citation(
                        kind=CitationKind.clause_ref,
                        ref=ref,
                        source="policy_rules",
                    )
                )
                seen_clause_refs.add(ref)

    return tuple(cits)
