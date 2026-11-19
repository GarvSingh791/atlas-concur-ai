"""Regression snapshots of structured eval outputs.

The eval suite snapshots the *structured* outputs it cares about — M-A2
linkage triples, M-A3 labels, M-A1 resolved fields — to canonical JSON under
``evals/snapshots/``.  On every run the freshly-computed outputs are compared to
the committed snapshot; a mismatch FAILS the test with a structured diff and
requires a deliberate snapshot update (``update=True`` / ``ATLAS_EVAL_UPDATE``).
A decision change therefore surfaces as a reviewable regression, never an
auto-pass.

Canonical form: ``json.dumps(..., sort_keys=True, indent=2)`` with a trailing
newline.  Keys are sorted so dict ordering cannot cause spurious diffs; floats
are rounded by the caller before snapshotting (the harness rounds metrics).

API:
  - ``canonical_json(obj)``                  -> deterministic JSON string
  - ``snapshot_path(name)``                  -> evals/snapshots/<name>.json
  - ``load_snapshot(name)``                  -> parsed committed snapshot | None
  - ``compare_snapshot(name, current, *, update=False)`` -> SnapshotResult
  - ``assert_snapshot(name, current, *, update=False)``  -> raises on mismatch
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SnapshotMismatch",
    "SnapshotResult",
    "SNAPSHOT_DIR",
    "canonical_json",
    "snapshot_path",
    "load_snapshot",
    "compare_snapshot",
    "assert_snapshot",
]

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "snapshots"

# Env flag the entrypoint can honor to regenerate snapshots deliberately.
_UPDATE_ENV = "ATLAS_EVAL_UPDATE"


class SnapshotMismatch(AssertionError):
    """Raised when current output diverges from the committed snapshot."""


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of a snapshot comparison."""

    name: str
    matched: bool
    created: bool  # snapshot did not exist and was written (update mode)
    updated: bool  # snapshot existed, differed, and was rewritten (update mode)
    diff: str | None  # human-readable diff when matched is False (non-update)


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to deterministic, diff-friendly JSON (trailing newline)."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"


def snapshot_path(name: str) -> Path:
    """Path to the committed snapshot file for ``name`` (no extension needed)."""
    safe = name if name.endswith(".json") else f"{name}.json"
    return SNAPSHOT_DIR / safe


def load_snapshot(name: str) -> Any | None:
    """Load and parse the committed snapshot, or ``None`` if it does not exist."""
    path = snapshot_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _want_update(explicit: bool) -> bool:
    if explicit:
        return True
    return os.environ.get(_UPDATE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _diff(expected: str, actual: str, name: str) -> str:
    import difflib

    lines = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"committed/{name}.json",
        tofile=f"current/{name}.json",
    )
    return "".join(lines)


def compare_snapshot(name: str, current: Any, *, update: bool = False) -> SnapshotResult:
    """Compare ``current`` against the committed snapshot ``name``.

    In update mode (``update=True`` or ``ATLAS_EVAL_UPDATE`` truthy) the snapshot
    is (re)written and the result is reported as matched.  Otherwise a missing or
    differing snapshot yields ``matched=False`` with a populated ``diff``.
    """
    path = snapshot_path(name)
    current_str = canonical_json(current)
    do_update = _want_update(update)

    if not path.exists():
        if do_update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(current_str, encoding="utf-8")
            return SnapshotResult(name=name, matched=True, created=True, updated=False, diff=None)
        return SnapshotResult(
            name=name,
            matched=False,
            created=False,
            updated=False,
            diff=(
                f"No committed snapshot at {path}. "
                f"Run with {_UPDATE_ENV}=1 (or update=True) to create it."
            ),
        )

    committed_str = path.read_text(encoding="utf-8")
    if committed_str == current_str:
        return SnapshotResult(name=name, matched=True, created=False, updated=False, diff=None)

    if do_update:
        path.write_text(current_str, encoding="utf-8")
        return SnapshotResult(name=name, matched=True, created=False, updated=True, diff=None)

    return SnapshotResult(
        name=name,
        matched=False,
        created=False,
        updated=False,
        diff=_diff(committed_str, current_str, name),
    )


def assert_snapshot(name: str, current: Any, *, update: bool = False) -> SnapshotResult:
    """Like ``compare_snapshot`` but raises ``SnapshotMismatch`` on a mismatch."""
    result = compare_snapshot(name, current, update=update)
    if not result.matched:
        raise SnapshotMismatch(
            f"Snapshot {name!r} does not match the committed snapshot. "
            f"This is a deliberate-review regression gate; if the change is intended, "
            f"re-run with {_UPDATE_ENV}=1.\n\n{result.diff}"
        )
    return result
