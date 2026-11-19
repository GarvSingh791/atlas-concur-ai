"""Receipt Analysis Pipeline (P4-T11, FR-R1..R5).

Public surface
--------------
run_receipt_pipeline  — main entry point; updates the receipt row (flush-only).
apply_user_correction — targeted field update; flips provenance to 'user_edited'.
release_inbox_draft   — release a receipt's auto-drafted inbox entry (C7-22).

Key dataclasses (re-exported for callers)
-----------------------------------------
RoutingDecision       — output of core OCR + confidence routing.
GapFillResult         — output of LLM gap-fill step.
"""

from __future__ import annotations

from ai_agents.receipt_pipeline.core import RoutingDecision
from ai_agents.receipt_pipeline.correction import apply_user_correction
from ai_agents.receipt_pipeline.gap_fill import GapFillResult
from ai_agents.receipt_pipeline.inbox import RECEIPT_INBOX_TITLE, release_inbox_draft
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline

__all__ = [
    "apply_user_correction",
    "release_inbox_draft",
    "RECEIPT_INBOX_TITLE",
    "run_receipt_pipeline",
    "GapFillResult",
    "RoutingDecision",
]
