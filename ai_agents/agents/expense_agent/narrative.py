"""Optional LLM-based narrative enrichment for the Expense Agent (P4-T10).

Generates plain-language explanations for flags and entries via the LLM provider.
Gracefully degrades to templated explanations on ProviderError (NFR-R1).
"""

from __future__ import annotations

import logging

from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.presubmit import PreSubmitFlag
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

_NARRATIVE_PROMPT = """\
You are a corporate expense advisor. In 2-3 sentences, summarize the following \
expense report highlights and any policy concerns. Be direct and concise.

Report title: {title}
Total amount: {total_amount} {currency}
Entry count: {entry_count}
Blocking flags: {blocking_count}
Advisory flags: {advisory_count}

Key flags:
{flags_summary}"""


def _templated_summary(report: DraftReport, flags: list[PreSubmitFlag]) -> str:
    """Generate a deterministic templated summary."""
    blocking = [f for f in flags if f.severity == "blocking"]
    advisory = [f for f in flags if f.severity == "advisory"]

    lines = [
        f"Draft expense report '{report.title}' with {len(report.entries)} entries "
        f"totaling {report.total_amount:.2f} {report.currency}."
    ]
    if blocking:
        lines.append(
            f"{len(blocking)} blocking issue(s) require attention before submission: "
            + ", ".join(f.flag_type for f in blocking[:3])
            + ("..." if len(blocking) > 3 else ".")
        )
    if advisory:
        lines.append(
            f"{len(advisory)} advisory note(s): "
            + ", ".join(f.flag_type for f in advisory[:3])
            + ("..." if len(advisory) > 3 else ".")
        )
    if not blocking and not advisory:
        lines.append("No policy issues detected.")
    return " ".join(lines)


def enrich_with_narrative(
    report: DraftReport,
    flags: list[PreSubmitFlag],
    *,
    provider: LlmProvider,
) -> tuple[str, bool]:
    """Generate an LLM narrative summary for the expense report.

    Returns:
        (narrative_text, degraded) where degraded=True means LLM failed
        and the templated fallback was used.
    """
    blocking = [f for f in flags if f.severity == "blocking"]
    advisory = [f for f in flags if f.severity == "advisory"]

    flags_summary_lines = []
    for f in flags[:5]:  # Limit to avoid token explosion
        flags_summary_lines.append(f"- [{f.severity}] {f.entry_ref}: {f.reason[:100]}")
    flags_summary = "\n".join(flags_summary_lines) or "(none)"

    prompt = _NARRATIVE_PROMPT.format(
        title=report.title,
        total_amount=f"{report.total_amount:.2f}",
        currency=report.currency,
        entry_count=len(report.entries),
        blocking_count=len(blocking),
        advisory_count=len(advisory),
        flags_summary=flags_summary,
    )

    try:
        response = provider.complete([LlmMessage(role="user", content=prompt)])
        narrative = response.text.strip() or _templated_summary(report, flags)
        return narrative, False
    except ProviderError as exc:
        logger.warning(
            "ExpenseAgent narrative: ProviderError(%s); using templated fallback (NFR-R1).",
            exc.code,
        )
        return _templated_summary(report, flags), True
