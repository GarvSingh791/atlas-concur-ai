"""Reusable harness library for the Atlas AI-eval suite.

Modules
-------
loaders       Dataset case dataclasses + ``load_*`` parsers + a validation CLI.
normalizers   ``normalize_money`` / ``normalize_date`` / ``normalize_text``.
metrics       confusion matrix, per-class & binary P/R/F1, recall@k/precision@k/hit@k,
              per-field M-A1 metrics, and ``write_report``.
policy_decision  ``classify(PolicyVerdict) -> {in_policy|violation|needs_review}`` —
                 the documented 3-class mapping that backs the M-A3 hard gate.
snapshot      canonical-JSON serialization + compare-to-committed regression helper.

Everything here is pure / deterministic so the CI-gated path can run against the
local provider with no network.
"""

from __future__ import annotations
