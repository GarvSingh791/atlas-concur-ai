"""Hermetic test: ai_agents must be importable without backend.

Uses a subprocess so that the import check is isolated from the main pytest
process (which may already have backend in sys.modules).

Verifies that importing every public ai_agents module does NOT pull in any
backend.* module — enforcing the layering contract that ai_agents is testable
and runnable independently of FastAPI / backend application code.
"""

from __future__ import annotations

import subprocess
import sys

_IMPORT_SCRIPT = """
import sys

# Import every public ai_agents module.
import ai_agents
import ai_agents.context
import ai_agents.exceptions
import ai_agents.decision_log
import ai_agents.decision_log.record
import ai_agents.decision_log.minimizer
import ai_agents.decision_log.writer
import ai_agents.guardrails
import ai_agents.guardrails.citations
import ai_agents.guardrails.actions
import ai_agents.guardrails.reflection
import ai_agents.tools
import ai_agents.tools.base
import ai_agents.tools.inventory
import ai_agents.tools.policy_eval
import ai_agents.tools.policy_retrieval
import ai_agents.tools.profile
import ai_agents.tools.itinerary_calendar
import ai_agents.rag
import ai_agents.rag.chunker
import ai_agents.rag.indexer
import ai_agents.rag.reindex
import ai_agents.rag.retrieval
import ai_agents.rag.calibration
import ai_agents.rag.rerank
import ai_agents.rag.contextual
import ai_agents.rag.query_rewrite
import ai_agents.rag.grounding
import ai_agents.orchestrator
import ai_agents.orchestrator.envelopes
import ai_agents.orchestrator.contracts
import ai_agents.orchestrator.registry
import ai_agents.orchestrator.classifier
import ai_agents.orchestrator.orchestrator
import ai_agents.agents
import ai_agents.agents.policy_eval_handler
import ai_agents.agents.policy_qa
import ai_agents.agents.policy_authoring
import ai_agents.agents.policy_agent
import ai_agents.agents.approval_agent
import ai_agents.agents.expense_agent
import ai_agents.agents.expense_agent.rule_categorizer
import ai_agents.agents.expense_agent.categorize_handler

backend_modules = sorted(m for m in sys.modules if m.startswith("backend"))
if backend_modules:
    print("FAIL: backend modules were imported by ai_agents:", flush=True)
    for m in backend_modules:
        print(f"  {m}", flush=True)
    sys.exit(1)

print("OK: no backend modules in sys.modules after importing ai_agents", flush=True)
sys.exit(0)
"""


def test_ai_agents_imports_without_backend() -> None:
    """Importing all ai_agents modules must not pull in any backend.* module."""
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ai_agents imported backend modules.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
