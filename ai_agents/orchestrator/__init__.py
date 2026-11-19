"""ai_agents.orchestrator — deterministic intent-classification and routing layer (P4-T7).

Front door of the Atlas intelligence plane (FR-A1).  The Orchestrator classifies
incoming tasks, decides a route (deterministic vs llm), dispatches to the correct
registered handler/agent, and writes one ai_decision_logs row per task (M-R4).

Public surface:
    Orchestrator       — the main entry point; call handle_task().
    AgentRegistry      — register agents + deterministic handlers; inject into Orchestrator.
    IntentClassifier   — deterministic classifier (injectable as Orchestrator strategy).
    Agent              — protocol every specialized agent (P4-T8+) must implement.
    DeterministicHandler — protocol for no-LLM task handlers.
    Task               — incoming request envelope.
    Classification     — classifier output (internal; exposed for testing).
    RoutingDecision    — routing choice (target agent, route, task_type, confidence).
    AgentResult        — what a handler/agent returns.
    AgentResultStatus  — ok / unavailable / needs_review.
    OrchestratorOutcome — full result returned to the caller.

Layering contract (INVARIANT):
    ai_agents.orchestrator imports ONLY ai_agents.*, config/, database.models, stdlib.
    NEVER import backend/.
"""

from ai_agents.orchestrator.classifier import IntentClassifier
from ai_agents.orchestrator.contracts import Agent, DeterministicHandler
from ai_agents.orchestrator.envelopes import (
    AgentResult,
    AgentResultStatus,
    Classification,
    OrchestratorOutcome,
    RoutingDecision,
    Task,
)
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry

__all__ = [
    "Agent",
    "AgentRegistry",
    "AgentResult",
    "AgentResultStatus",
    "Classification",
    "DeterministicHandler",
    "IntentClassifier",
    "Orchestrator",
    "OrchestratorOutcome",
    "RoutingDecision",
    "Task",
]
