"""ai_agents.tools — grounding tool layer (P4-T4).

Five read-only, deterministic, LLM-free tools agents call to "fetch the truth":

  InventoryTool       — hermetic mock travel inventory search
  PolicyEvalTool      — canonical deterministic policy rule evaluator
  PolicyRetrievalTool — pgvector ANN retrieval (RAG) over policy_embeddings
  ProfileTool         — user profile + travel history
  ItineraryCalendarTool — trip segments + calendar events

Tool registry and routing are P4-T7.  Backend /policy/evaluate delegates into
PolicyEvalTool (P4-T13).  Agents P4-T8..T12 consume these tools.

LAYERING LAW: this package imports only config/, database.models, services/, stdlib.
Never import backend/.
"""

from ai_agents.tools.base import GroundingTool, ToolError
from ai_agents.tools.inventory import (
    InventoryIntent,
    InventoryOption,
    InventoryResult,
    InventoryTool,
)
from ai_agents.tools.itinerary_calendar import (
    CalendarEntry,
    ItineraryCalendarTool,
    ItineraryIntent,
    ItineraryResult,
    ItinerarySegment,
)
from ai_agents.tools.policy_eval import (
    EvalItem,
    PolicyEvalTool,
    PolicyVerdict,
    RuleVerdict,
)
from ai_agents.tools.policy_retrieval import (
    PolicyRetrievalTool,
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)
from ai_agents.tools.profile import (
    ProfileIntent,
    ProfileResult,
    ProfileTool,
    TripSummary,
)

__all__ = [
    # base
    "ToolError",
    "GroundingTool",
    # inventory
    "InventoryTool",
    "InventoryIntent",
    "InventoryOption",
    "InventoryResult",
    # policy eval
    "PolicyEvalTool",
    "EvalItem",
    "RuleVerdict",
    "PolicyVerdict",
    # policy retrieval
    "PolicyRetrievalTool",
    "RetrievalIntent",
    "RetrievedChunk",
    "RetrievalResult",
    # profile
    "ProfileTool",
    "ProfileIntent",
    "TripSummary",
    "ProfileResult",
    # itinerary + calendar
    "ItineraryCalendarTool",
    "ItineraryIntent",
    "ItinerarySegment",
    "CalendarEntry",
    "ItineraryResult",
]
