"""DecisionRecord — structured input to the decision-log writer.

One DecisionRecord maps 1:1 to one ai_decision_logs row.  The writer
validates enum membership and confidence range before persisting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome


@dataclass
class DecisionRecord:
    """Describes one AI agent invocation before it is persisted.

    Required fields mirror the NOT NULL columns on ai_decision_logs (excluding
    organization_id and user_id, which come from AgentContext).

    Optional fields are None when not applicable (e.g. model_provider is None
    on the deterministic route; reflection_outcome is None until P4-T3 adds the
    critic loop).
    """

    agent: AiAgent
    task_type: str
    route: AiRoute
    model_provider: str | None = None
    input_summary: dict[str, Any] | None = None
    tools_used: dict[str, Any] | None = None
    output_summary: dict[str, Any] | None = None
    confidence: float | None = None
    # Accepted as a pass-through field value — NO reflection logic here (P4-T3).
    reflection_outcome: ReflectionOutcome | None = None
    latency_ms: int | None = None
