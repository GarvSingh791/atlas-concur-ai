"""ai_agents — Atlas AI orchestration & guardrail substrate.

Layering contract (INVARIANT):
  ai_agents imports ONLY config/, database.models, services/, and stdlib.
  NEVER import backend/ (no FastAPI, no application errors, no backend RBAC, no backend logging).
  ai_agents must be importable and testable without backend present.

Sessions and permission sets are INJECTED by the caller (eventually
backend/modules/ai in P4-T13).  ai_agents never opens a DB session and never
resolves a user or permission set from the database itself.
"""
