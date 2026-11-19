"""UAT2 hardening — the receipt machine reading and the reuse-strength rule (hermetic).

The reuse check compares what receipts READ (the OCR snapshot, or a legacy row's
still-deterministic fields), never retyped values:
  * contradicting machine readings (two read dates / suppliers that differ) rule
    a match out — a typed value never can;
  * the same read total with a read date or supplier that agrees → STRONG;
  * only a total links them (typed / unread / legacy fields) → WEAK.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from ai_agents.agents.expense_agent.presubmit import (
    _STRONG,
    _WEAK,
    _receipt_evidence,
    _reuse_strength,
    _weak_basis,
    receipt_total_covers,
)
from ai_agents.receipt_pipeline.evidence import (
    OCR_SNAPSHOT_KEY,
    SHARED_EXPENSES_KEY,
    build_ocr_snapshot,
    machine_reading,
    machine_total,
    set_shared_expense_ids,
    shared_expense_ids,
    with_ocr_snapshot,
)
from database.models.receipt import Receipt

DET, EDITED, AI = "deterministic", "user_edited", "ai_inferred"
_READ = {"amount": 60.0, "date": "2026-06-10", "vendor": "Corner Deli", "currency": "USD"}


def _ev(
    fields: dict[str, Any] | None = None,
    prov: dict[str, str] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> Any:
    grounding = {OCR_SNAPSHOT_KEY: snapshot} if snapshot is not None else None
    ev = _receipt_evidence(
        uuid.uuid4(),
        {**_READ, **(fields or {})},
        {"amount": DET, "date": DET, "vendor": DET, **(prov or {})},
        grounding,
    )
    assert ev is not None
    return ev


def test_snapshot_keeps_only_deterministic_amount_date_vendor() -> None:
    snap = build_ocr_snapshot(
        {**_READ, "location": "NYC"},
        {"amount": DET, "date": AI, "vendor": DET, "location": DET},
    )
    assert snap == {"amount": 60.0, "vendor": "Corner Deli"}
    ctx = with_ocr_snapshot({"segments": [1], SHARED_EXPENSES_KEY: ["x"]}, snap)
    assert ctx["segments"] == [1] and ctx[SHARED_EXPENSES_KEY] == ["x"]


def test_a_retyped_copy_still_reads_the_same_through_its_snapshot() -> None:
    original = _ev(snapshot=dict(_READ))
    copy = _ev(
        fields={"vendor": "SkyPark", "date": "2026-06-15", "amount": 61.0},
        prov={"vendor": EDITED, "date": EDITED, "amount": EDITED},
        snapshot=dict(_READ),
    )
    assert _reuse_strength(copy, original) == _STRONG


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # legacy, both fully read and equal → strong (unchanged behaviour)
        ({}, {}, _STRONG),
        # read dates differ → two purchases, whatever else
        ({}, {"fields": {"date": "2026-06-12"}}, None),
        # read suppliers differ → two purchases
        ({}, {"fields": {"vendor": "Shell"}}, None),
        # same read total, retyped vendor + date: nothing to confirm → WEAK (was: pass)
        ({}, {"prov": {"vendor": EDITED, "date": EDITED}}, _WEAK),
        # typed total on one side, same read date + supplier → WEAK (was: trusted)
        ({"prov": {"amount": EDITED}}, {}, _WEAK),
        # typed total that differs: no shared total at all → None
        ({"prov": {"amount": EDITED}, "fields": {"amount": 59.0}}, {}, None),
        # same read date, one supplier unread → STRONG (fail closed, unchanged)
        ({}, {"prov": {"vendor": AI}}, _STRONG),
        # date unread on one side, suppliers agree → STRONG
        ({"prov": {"date": AI}}, {}, _STRONG),
        # revision: OCR MISread the copy's total (59.00), the claimant typed the
        # original's 60.00, supplier read the same, copy date unread → WEAK (was:
        # None — a differing machine total silently ruled the match out)
        (
            {
                "fields": {"amount": 60.0},
                "prov": {"amount": EDITED},
                "snapshot": {"amount": 59.0, "vendor": "Corner Deli"},
            },
            {},
            _WEAK,
        ),
        # same, with both read dates agreeing and one supplier unread → WEAK
        (
            {
                "fields": {"amount": 60.0},
                "prov": {"amount": EDITED},
                "snapshot": {"amount": 59.0, "date": "2026-06-10"},
            },
            {},
            _WEAK,
        ),
        # but a contradicting machine reading still rules it out → None
        (
            {
                "fields": {"amount": 60.0},
                "prov": {"amount": EDITED},
                "snapshot": {"amount": 59.0, "date": "2026-06-12"},
            },
            {},
            None,
        ),
    ],
)
def test_reuse_strength(a: dict[str, Any], b: dict[str, Any], expected: str | None) -> None:
    assert _reuse_strength(_ev(**a), _ev(**b)) == expected
    assert _reuse_strength(_ev(**b), _ev(**a)) == expected


def test_the_weak_reason_names_what_was_typed() -> None:
    """Revision: the reviewer-facing text said "its date or vendor was typed"
    even when it was the TOTAL that was typed."""
    original = _ev()
    typed_total = _ev(prov={"amount": EDITED})
    typed_vendor = _ev(prov={"vendor": EDITED, "date": EDITED})
    assert _weak_basis(typed_total, original).startswith("a total was typed")
    assert _weak_basis(typed_vendor, original).startswith("its date or vendor was typed")


def _receipt(**kwargs: Any) -> Receipt:
    return Receipt(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        uploaded_by=uuid.uuid4(),
        image_url="k",
        **kwargs,
    )


def test_machine_total_never_uses_a_typed_total() -> None:
    legacy = _receipt(extracted_fields=dict(_READ), field_provenance={"amount": DET})
    assert machine_total(legacy) == Decimal("60.00")
    typed = _receipt(extracted_fields=dict(_READ), field_provenance={"amount": EDITED})
    assert machine_total(typed) is None
    snap = _receipt(
        extracted_fields={**_READ, "amount": 999.0},
        field_provenance={"amount": EDITED},
        grounding_context={OCR_SNAPSHOT_KEY: {"amount": 60.0}},
    )
    assert machine_total(snap) == Decimal("60.00")


def test_share_list_rewrites_never_touch_the_snapshot() -> None:
    primary, sibling = uuid.uuid4(), uuid.uuid4()
    receipt = _receipt(expense_id=primary, grounding_context={OCR_SNAPSHOT_KEY: {"amount": 9}})
    set_shared_expense_ids(receipt, [sibling, primary, sibling])
    assert shared_expense_ids(receipt) == [sibling]
    assert receipt.grounding_context[OCR_SNAPSHOT_KEY] == {"amount": 9}  # type: ignore[index]
    set_shared_expense_ids(receipt, [])
    assert receipt.grounding_context == {OCR_SNAPSHOT_KEY: {"amount": 9}}


def test_machine_reading_is_the_snapshot_or_untyped_legacy_fields() -> None:
    """UAT2 defect F: the reviewer sees what OCR READ, never a retyped value."""
    snapped = Receipt(
        extracted_fields={**_READ, "vendor": "Typed Name", "amount": 999.0},
        field_provenance={"amount": EDITED, "date": DET, "vendor": EDITED},
        grounding_context={OCR_SNAPSHOT_KEY: {"amount": 60.0, "vendor": "Corner Deli"}},
    )
    assert machine_reading(snapped) == {
        "vendor": "Corner Deli",
        "date": None,  # not in the snapshot (read by AI at the time)
        "amount": Decimal("60.00"),
    }
    legacy = Receipt(
        extracted_fields={**_READ, "date": "2026-06-10T00:00:00"},
        field_provenance={"amount": EDITED, "date": DET, "vendor": AI},
        grounding_context=None,
    )
    assert machine_reading(legacy) == {"vendor": None, "date": "2026-06-10", "amount": None}


@pytest.mark.parametrize(
    ("total", "claimed", "covers"),
    [(205.5, 205.5, True), (8.75, 205.5, False), (100, 125, True), (100, 125.02, False)],
)
def test_receipt_total_covers_uses_the_tip_slack(
    total: float, claimed: float, covers: bool
) -> None:
    assert receipt_total_covers(total, claimed) is covers
