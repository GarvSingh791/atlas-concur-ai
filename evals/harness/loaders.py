"""Dataset loaders + case dataclasses + a validation CLI.

Each dataset is a JSONL file (one JSON object per line) under ``evals/datasets/``,
except ``policy_rules.json`` which is a single JSON array mirroring
``database/seeds/policy_content.py`` RULES.

Every loader:
  - parses each line / element into a typed dataclass,
  - validates the required keys for that schema and raises a clear,
    line-numbered error on a malformed record,
  - returns ``list[<Case>]``.

The dataclasses ARE the dataset contract the dataset agents and entrypoint
authors build against — keep them stable.

CLI (lets dataset agents self-validate their files)::

    python -m evals.harness.loaders <name> <path>

where ``<name>`` is one of ``m_a1 | m_a2 | m_a3 | rag | policy_rules``.
Prints the parsed record count and exits 0 on success, non-zero on the first
malformed record.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DatasetError",
    "MA1Case",
    "MA2Case",
    "MA3Case",
    "RagCase",
    "CorpusRagCase",
    "PolicyRuleDef",
    "load_m_a1",
    "load_m_a2",
    "load_m_a3",
    "load_rag",
    "load_rag_corpus",
    "load_policy_rules",
]


class DatasetError(ValueError):
    """Raised when a dataset file is malformed (missing keys, bad JSON, ...)."""


# ---------------------------------------------------------------------------
# Case dataclasses (the dataset contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MA1Case:
    """One M-A1 receipt-extraction case.

    ``ocr`` carries the OCR stub payload (``fields`` + ``field_confidence``).
    ``itinerary`` / ``gap_fill_json`` are optional grounding for gap-fill cases.
    ``gold`` carries the expected resolved extraction:
    ``extracted_fields`` / ``field_provenance`` / ``processing_path``.
    """

    id: str
    category: str
    currency: str
    tags: list[str]
    ocr: dict[str, Any]
    gold: dict[str, Any]
    itinerary: dict[str, Any] | None = None
    gap_fill_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class MA2Case:
    """One M-A2 smart-match case.

    ``ingest`` is the ``IngestedData`` payload (receipts / card_txns / segments /
    trip_id / unprocessed_receipts).  ``gold`` carries the expected linkage:
    ``matches`` (list of {receipt_id?, card_txn_id?, segment_id?, is_duplicate?}),
    ``unmatched_receipts`` and ``unmatched_card_txns``.
    """

    id: str
    tags: list[str]
    ingest: dict[str, Any]
    gold: dict[str, Any]


@dataclass(frozen=True)
class MA3Case:
    """One M-A3 policy-decision case.

    ``item`` is the ``EvalItem`` payload; ``gold_label`` is the expected 3-class
    label in {in_policy, violation, needs_review} (see policy_decision.classify).
    """

    id: str
    tags: list[str]
    item: dict[str, Any]
    gold_label: str


@dataclass(frozen=True)
class RagCase:
    """One RAG retrieval case.

    ``gold_clause_refs`` are the ground-truth clause refs that SHOULD be retrieved;
    ``expect`` is ``grounded`` (clauses retrievable) or ``silent`` (off-topic).
    """

    id: str
    query: str
    gold_clause_refs: list[str]
    expect: str
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CorpusRagCase:
    """One multi-document RAG gold case (R0-T5).

    ``expected_unit_ids`` are the within-document unit ids that answer the query and
    ``expected_doc_ids`` the documents they live in (any-of for single-doc queries;
    ``cross_document`` items genuinely need >=2 documents).  ``answerable`` False marks
    an out-of-corpus query that MUST abstain (both expected lists empty).  ``type`` is
    one of rule / conversational / howto / threshold / cross_document / trip_planning /
    unanswerable.  Structural validation only here; cross-checking the ids against the
    real corpus is the dataset-integrity test's job.
    """

    id: str
    query: str
    answerable: bool
    expected_doc_ids: list[str]
    expected_unit_ids: list[str]
    type: str
    cross_document: bool
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyRuleDef:
    """One policy rule definition mirroring ``policy_content.py`` RULES.

    Used to build in-memory ``PolicyRule`` rows (Tier-1, no DB) and to seed
    (Tier-2).  ``rule_type`` / ``applies_to`` must be valid enum values;
    ``definition`` is the machine-evaluable JSONB body.
    """

    rule_type: str
    applies_to: str
    definition: dict[str, Any]
    source_clause_ref: str


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_VALID_GOLD_LABELS = frozenset({"in_policy", "violation", "needs_review"})
_VALID_EXPECT = frozenset({"grounded", "silent"})
_VALID_CORPUS_TYPES = frozenset(
    {
        "rule",
        "conversational",
        "howto",
        "threshold",
        "cross_document",
        "trip_planning",
        "unanswerable",
    }
)
_VALID_RULE_TYPES = frozenset(
    {
        "amount_cap",
        "category_allow",
        "cabin_class",
        "per_diem",
        "preferred_supplier",
        "receipt_required",
    }
)
_VALID_APPLIES_TO = frozenset({"flight", "hotel", "rail", "car", "meals", "all"})


def _iter_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Read a JSONL file -> list of (1-based line number, object).

    Blank lines and ``#``-prefixed comment lines are skipped.
    """
    if not path.exists():
        raise DatasetError(f"Dataset file not found: {path}")
    records: list[tuple[int, dict[str, Any]]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(
                    f"{path}:{lineno}: each line must be a JSON object, got {type(obj).__name__}"
                )
            records.append((lineno, obj))
    return records


def _require(obj: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise DatasetError(f"{where}: missing required key(s): {', '.join(missing)}")


def _require_subkeys(
    obj: dict[str, Any], parent: str, keys: tuple[str, ...], *, where: str
) -> None:
    sub = obj.get(parent)
    if not isinstance(sub, dict):
        raise DatasetError(f"{where}: '{parent}' must be an object")
    missing = [k for k in keys if k not in sub]
    if missing:
        raise DatasetError(f"{where}: '{parent}' missing key(s): {', '.join(missing)}")


def _ensure_unique_ids(cases: list[Any], *, path: Path) -> None:
    seen: set[str] = set()
    for case in cases:
        cid = case.id
        if cid in seen:
            raise DatasetError(f"{path}: duplicate case id {cid!r}")
        seen.add(cid)


# ---------------------------------------------------------------------------
# M-A1
# ---------------------------------------------------------------------------

_RECEIPT_FIELDS = ("amount", "currency", "date", "vendor", "location", "expense_type")
_VALID_PROCESSING_PATHS = frozenset({"deterministic", "agent_gap_fill"})
_VALID_PROVENANCE = frozenset({"deterministic", "ai_inferred"})


def load_m_a1(path: str | Path) -> list[MA1Case]:
    """Load and validate the M-A1 extraction dataset (JSONL)."""
    path = Path(path)
    cases: list[MA1Case] = []
    for lineno, obj in _iter_jsonl(path):
        where = f"{path}:{lineno}"
        _require(obj, ("id", "category", "currency", "ocr", "gold"), where=where)
        _require_subkeys(obj, "ocr", ("fields", "field_confidence"), where=where)
        _require_subkeys(
            obj, "gold", ("extracted_fields", "field_provenance", "processing_path"), where=where
        )

        gold = obj["gold"]
        path_val = gold["processing_path"]
        if path_val not in _VALID_PROCESSING_PATHS:
            raise DatasetError(
                f"{where}: gold.processing_path must be one of "
                f"{sorted(_VALID_PROCESSING_PATHS)}, got {path_val!r}"
            )
        for fname, prov in gold["field_provenance"].items():
            if prov not in _VALID_PROVENANCE:
                raise DatasetError(
                    f"{where}: gold.field_provenance[{fname!r}] must be one of "
                    f"{sorted(_VALID_PROVENANCE)}, got {prov!r}"
                )
        # Every gold extracted field must be a known receipt field key.
        for fname in gold["extracted_fields"]:
            if fname not in _RECEIPT_FIELDS:
                raise DatasetError(
                    f"{where}: gold.extracted_fields has unknown field {fname!r}; "
                    f"expected one of {list(_RECEIPT_FIELDS)}"
                )

        cases.append(
            MA1Case(
                id=str(obj["id"]),
                category=str(obj["category"]),
                currency=str(obj["currency"]),
                tags=list(obj.get("tags", [])),
                ocr=obj["ocr"],
                gold=gold,
                itinerary=obj.get("itinerary"),
                gap_fill_json=obj.get("gap_fill_json"),
            )
        )
    _ensure_unique_ids(cases, path=path)
    return cases


# ---------------------------------------------------------------------------
# M-A2
# ---------------------------------------------------------------------------


def load_m_a2(path: str | Path) -> list[MA2Case]:
    """Load and validate the M-A2 smart-match dataset (JSONL)."""
    path = Path(path)
    cases: list[MA2Case] = []
    for lineno, obj in _iter_jsonl(path):
        where = f"{path}:{lineno}"
        _require(obj, ("id", "ingest", "gold"), where=where)
        _require_subkeys(obj, "ingest", ("receipts", "card_txns", "segments"), where=where)
        _require_subkeys(
            obj, "gold", ("matches", "unmatched_receipts", "unmatched_card_txns"), where=where
        )

        ingest = obj["ingest"]
        for coll in ("receipts", "card_txns", "segments"):
            if not isinstance(ingest[coll], list):
                raise DatasetError(f"{where}: ingest.{coll} must be a list")
        for r in ingest["receipts"]:
            _require(
                r, ("receipt_id", "amount", "currency", "date"), where=f"{where} ingest.receipt"
            )
        for t in ingest["card_txns"]:
            _require(
                t, ("txn_id", "amount", "currency", "txn_date"), where=f"{where} ingest.card_txn"
            )
        for s in ingest["segments"]:
            _require(
                s,
                ("segment_id", "trip_id", "segment_type", "amount", "currency"),
                where=f"{where} ingest.segment",
            )
        if not isinstance(obj["gold"]["matches"], list):
            raise DatasetError(f"{where}: gold.matches must be a list")

        cases.append(
            MA2Case(
                id=str(obj["id"]),
                tags=list(obj.get("tags", [])),
                ingest=ingest,
                gold=obj["gold"],
            )
        )
    _ensure_unique_ids(cases, path=path)
    return cases


# ---------------------------------------------------------------------------
# M-A3
# ---------------------------------------------------------------------------

_VALID_ITEM_TYPES = frozenset({"flight", "hotel", "rail", "car", "meals", "receipt"})


def load_m_a3(path: str | Path) -> list[MA3Case]:
    """Load and validate the M-A3 policy-decision dataset (JSONL)."""
    path = Path(path)
    cases: list[MA3Case] = []
    for lineno, obj in _iter_jsonl(path):
        where = f"{path}:{lineno}"
        _require(obj, ("id", "item", "gold_label"), where=where)
        if not isinstance(obj["item"], dict):
            raise DatasetError(f"{where}: 'item' must be an object")
        _require(obj["item"], ("item_type",), where=f"{where} item")
        item_type = obj["item"]["item_type"]
        if item_type not in _VALID_ITEM_TYPES:
            raise DatasetError(
                f"{where}: item.item_type must be one of {sorted(_VALID_ITEM_TYPES)}, "
                f"got {item_type!r}"
            )
        label = obj["gold_label"]
        if label not in _VALID_GOLD_LABELS:
            raise DatasetError(
                f"{where}: gold_label must be one of {sorted(_VALID_GOLD_LABELS)}, got {label!r}"
            )
        cases.append(
            MA3Case(
                id=str(obj["id"]),
                tags=list(obj.get("tags", [])),
                item=obj["item"],
                gold_label=label,
            )
        )
    _ensure_unique_ids(cases, path=path)
    return cases


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------


def load_rag(path: str | Path) -> list[RagCase]:
    """Load and validate the RAG retrieval dataset (JSONL)."""
    path = Path(path)
    cases: list[RagCase] = []
    for lineno, obj in _iter_jsonl(path):
        where = f"{path}:{lineno}"
        _require(obj, ("id", "query", "gold_clause_refs", "expect"), where=where)
        if not isinstance(obj["gold_clause_refs"], list):
            raise DatasetError(f"{where}: gold_clause_refs must be a list")
        expect = obj["expect"]
        if expect not in _VALID_EXPECT:
            raise DatasetError(
                f"{where}: expect must be one of {sorted(_VALID_EXPECT)}, got {expect!r}"
            )
        if expect == "grounded" and not obj["gold_clause_refs"]:
            raise DatasetError(f"{where}: a 'grounded' case must list >=1 gold_clause_refs")
        if expect == "silent" and obj["gold_clause_refs"]:
            raise DatasetError(f"{where}: a 'silent' case must have empty gold_clause_refs")
        cases.append(
            RagCase(
                id=str(obj["id"]),
                query=str(obj["query"]),
                gold_clause_refs=[str(c) for c in obj["gold_clause_refs"]],
                expect=expect,
                tags=list(obj.get("tags", [])),
            )
        )
    _ensure_unique_ids(cases, path=path)
    return cases


# ---------------------------------------------------------------------------
# RAG corpus (multi-document gold set, R0-T5)
# ---------------------------------------------------------------------------


def load_rag_corpus(path: str | Path) -> list[CorpusRagCase]:
    """Load and validate the multi-document RAG gold set (JSONL).

    Structural validation only (shape + internal consistency).  Cross-checking the
    unit/doc ids against the real corpus is the dataset-integrity test's job.
    """
    path = Path(path)
    cases: list[CorpusRagCase] = []
    for lineno, obj in _iter_jsonl(path):
        where = f"{path}:{lineno}"
        _require(
            obj,
            (
                "id",
                "query",
                "answerable",
                "expected_doc_ids",
                "expected_unit_ids",
                "type",
                "cross_document",
            ),
            where=where,
        )
        if not isinstance(obj["answerable"], bool):
            raise DatasetError(f"{where}: 'answerable' must be a boolean")
        if not isinstance(obj["cross_document"], bool):
            raise DatasetError(f"{where}: 'cross_document' must be a boolean")
        for key in ("expected_doc_ids", "expected_unit_ids"):
            if not isinstance(obj[key], list):
                raise DatasetError(f"{where}: '{key}' must be a list")
        typ = obj["type"]
        if typ not in _VALID_CORPUS_TYPES:
            raise DatasetError(
                f"{where}: type must be one of {sorted(_VALID_CORPUS_TYPES)}, got {typ!r}"
            )
        answerable = obj["answerable"]
        unit_ids = [str(u) for u in obj["expected_unit_ids"]]
        doc_ids = [str(d) for d in obj["expected_doc_ids"]]
        if answerable:
            if not unit_ids:
                raise DatasetError(f"{where}: an answerable case must list >=1 expected_unit_ids")
            if not doc_ids:
                raise DatasetError(f"{where}: an answerable case must list >=1 expected_doc_ids")
            if typ == "unanswerable":
                raise DatasetError(f"{where}: type 'unanswerable' must have answerable=false")
        else:
            if unit_ids or doc_ids:
                raise DatasetError(
                    f"{where}: an unanswerable case must have empty expected_unit_ids/doc_ids"
                )
            if typ != "unanswerable":
                raise DatasetError(f"{where}: an unanswerable case must have type 'unanswerable'")
        if obj["cross_document"] and len(doc_ids) < 2:
            raise DatasetError(f"{where}: a cross_document case needs >=2 expected_doc_ids")
        cases.append(
            CorpusRagCase(
                id=str(obj["id"]),
                query=str(obj["query"]),
                answerable=answerable,
                expected_doc_ids=doc_ids,
                expected_unit_ids=unit_ids,
                type=str(typ),
                cross_document=bool(obj["cross_document"]),
                tags=list(obj.get("tags", [])),
            )
        )
    _ensure_unique_ids(cases, path=path)
    return cases


# ---------------------------------------------------------------------------
# policy_rules.json
# ---------------------------------------------------------------------------


def load_policy_rules(path: str | Path) -> list[PolicyRuleDef]:
    """Load and validate ``policy_rules.json`` (a JSON array, not JSONL)."""
    path = Path(path)
    if not path.exists():
        raise DatasetError(f"Policy rules file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DatasetError(f"{path}: policy_rules.json must be a JSON array")

    rules: list[PolicyRuleDef] = []
    for idx, obj in enumerate(data):
        where = f"{path}[{idx}]"
        if not isinstance(obj, dict):
            raise DatasetError(f"{where}: each rule must be an object")
        _require(obj, ("rule_type", "applies_to", "definition", "source_clause_ref"), where=where)
        if obj["rule_type"] not in _VALID_RULE_TYPES:
            raise DatasetError(
                f"{where}: rule_type must be one of {sorted(_VALID_RULE_TYPES)}, "
                f"got {obj['rule_type']!r}"
            )
        if obj["applies_to"] not in _VALID_APPLIES_TO:
            raise DatasetError(
                f"{where}: applies_to must be one of {sorted(_VALID_APPLIES_TO)}, "
                f"got {obj['applies_to']!r}"
            )
        if not isinstance(obj["definition"], dict):
            raise DatasetError(f"{where}: definition must be an object")
        rules.append(
            PolicyRuleDef(
                rule_type=str(obj["rule_type"]),
                applies_to=str(obj["applies_to"]),
                definition=obj["definition"],
                source_clause_ref=str(obj["source_clause_ref"]),
            )
        )
    if not rules:
        raise DatasetError(f"{path}: policy_rules.json is empty")
    return rules


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_LOADERS: dict[str, Callable[[str | Path], list[Any]]] = {
    "m_a1": load_m_a1,
    "m_a2": load_m_a2,
    "m_a3": load_m_a3,
    "rag": load_rag,
    "rag_corpus": load_rag_corpus,
    "policy_rules": load_policy_rules,
}


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(
            "usage: python -m evals.harness.loaders <name> <path>\n"
            f"  <name> in {sorted(_LOADERS)}\n"
        )
        return 2
    name, path = argv
    loader = _LOADERS.get(name)
    if loader is None:
        sys.stderr.write(f"error: unknown loader {name!r}; choose from {sorted(_LOADERS)}\n")
        return 2
    try:
        cases = loader(path)
    except DatasetError as exc:
        sys.stderr.write(f"INVALID: {exc}\n")
        return 1
    sys.stdout.write(f"OK: {name} parsed {len(cases)} record(s) from {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
