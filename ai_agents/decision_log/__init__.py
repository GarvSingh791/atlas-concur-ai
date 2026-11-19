"""ai_agents.decision_log — decision-log writer and supporting types.

Public surface:
    DecisionRecord  — structured record describing one AI invocation
    write_decision_log — append one ai_decision_logs row (caller commits)
    latency_timer  — context manager that captures elapsed ms

Future consumer: backend/modules/ai (P4-T13) constructs a DecisionRecord and
calls write_decision_log within the same transaction as the business operation.
"""

from ai_agents.decision_log.record import DecisionRecord
from ai_agents.decision_log.writer import latency_timer, write_decision_log

__all__ = ["DecisionRecord", "latency_timer", "write_decision_log"]
