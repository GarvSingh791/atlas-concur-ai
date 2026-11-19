"""ai_agents.guardrails — grounding/citation contract, role-bound action check,
and the generator→critic reflection loop.

Public surface:
    Citation, CitationKind, GroundedResult, validate_grounding — FR-A4 citation
        contract; ensures confident claims are backed by evidence.
    AgentAction, ACTION_PERMISSION_MAP, check_action — NFR-SEC6 role-bound action
        check; blocks any action whose required permission key is absent from the
        injected AgentContext.
    ReflectionOutcome — re-export of database.models enum (passed|revised|rejected);
        maps 1:1 to ai_decision_logs.reflection_outcome.
    reflect, ReflectionResult, Critique, CriticVerdict, Hypothesizer, Critic —
        FR-A3 generator→critic bounded loop (P4-T3).
    GroundingCritic, GroundingFacts, LocationFact, LocationClaim — reference
        deterministic-first critic with optional LLM Stage 2.
    DEFAULT_MAX_ROUNDS — default iteration cap for reflect().

Future consumers:
    - check_action: agents (P4-T8+) call this before any state-changing operation.
    - validate_grounding / GroundedResult: policy agent (P4-T9), travel agent
      (P4-T8) when surfacing policy-constrained recommendations.
    - reflect / GroundingCritic: travel recommendation agent (P4-T9) and receipt
      gap-fill pipeline (P4-T11).
"""

from ai_agents.guardrails.actions import (
    ACTION_PERMISSION_MAP,
    AgentAction,
    check_action,
)
from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    Citation,
    CitationKind,
    GroundedResult,
    validate_grounding,
)
from ai_agents.guardrails.reflection import (
    DEFAULT_MAX_ROUNDS,
    Critic,
    CriticVerdict,
    Critique,
    GroundingCritic,
    GroundingFacts,
    Hypothesizer,
    LocationClaim,
    LocationFact,
    ReflectionResult,
    reflect,
)
from database.models.ai_decision_log import ReflectionOutcome

__all__ = [
    # citations
    "ACTION_PERMISSION_MAP",
    "AgentAction",
    "check_action",
    "Citation",
    "CitationKind",
    "DEFAULT_CITATION_THRESHOLD",
    "GroundedResult",
    "validate_grounding",
    # reflection loop
    "Critic",
    "Critique",
    "CriticVerdict",
    "DEFAULT_MAX_ROUNDS",
    "GroundingCritic",
    "GroundingFacts",
    "Hypothesizer",
    "LocationClaim",
    "LocationFact",
    "ReflectionOutcome",
    "ReflectionResult",
    "reflect",
]
