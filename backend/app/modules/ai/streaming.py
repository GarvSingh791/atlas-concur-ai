"""SSE frame formatting for the AI conversation endpoint (P4-T13 Slice 1).

The send_message endpoint runs the orchestrator fully (synchronous),
commits once, then replays the result as a list of SSE frames via
StreamingResponse.

SSE wire format:  event: <name>\\ndata: <json>\\n\\n
"""

from __future__ import annotations

import json
from typing import Any

from ai_agents.agents.policy_agent import presentable_policy_output
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def build_sse_events(
    result: AgentResult,
    *,
    task_type: str,
    route: str,
    confidence: float,
    explain_with_llm: bool,
    message_id: Any,
    sequence: int,
) -> list[str]:
    """Build the ordered list of SSE event strings from an AgentResult.

    Frame order:
      1. status       — always
      2. tool_activity — only if tool_calls are present in output
      3. result       — always
      4. narrative    — only if explain_with_llm and narrative/explanation exists
      5. done         — always
    """
    events: list[str] = []

    # 1. status frame
    events.append(
        _sse(
            "status",
            {
                "task_type": task_type,
                "route": route,
                "confidence": confidence,
                "status": result.status.value,
            },
        )
    )

    # 2. tool_activity frame (only when tool_calls in output)
    tool_calls = result.output.get("tool_calls") if result.output else None
    if tool_calls:
        events.append(_sse("tool_activity", {"tool_calls": tool_calls}))

    # 3. result frame (C7-06: a withheld needs_review policy draft is not leaked through
    #    answer_structured / citations; every other output passes through unchanged)
    frame_output, frame_citations = presentable_policy_output(result.output or {}, result.citations)
    events.append(
        _sse(
            "result",
            {
                "output": frame_output,
                "citations": [c.to_dict() for c in frame_citations],
            },
        )
    )

    # 4. narrative frames (chunked into 10-word chunks)
    if explain_with_llm:
        narrative: str | None = None
        if result.output:
            narrative = result.output.get("narrative") or result.output.get("explanation")
        if narrative:
            words = str(narrative).split()
            chunks = [" ".join(words[i : i + 10]) for i in range(0, len(words), 10)]
            for chunk in chunks:
                events.append(_sse("narrative", {"text": chunk}))

    # 5. done frame
    events.append(
        _sse(
            "done",
            {
                "needs_review": result.status == AgentResultStatus.needs_review,
                "message_id": str(message_id) if message_id is not None else None,
                "sequence": sequence,
            },
        )
    )

    return events
