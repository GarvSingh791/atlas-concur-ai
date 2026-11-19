"""C7R review — /policy/evaluate flags stay attributable to an entry (hermetic).

``entry_ref`` became a readable label (vendor, or category + amount + date) so flag
reasons never print a UUID.  Two entries with the same vendor would then be
indistinguishable, so the service makes labels unique within the subject and the
router copies each unit's ``entry_id`` onto its flags and item rows.

Regression proof: with ``_unique_entry_labels`` replaced by the plain
``entry_label`` mapping, ``test_duplicate_vendors_get_distinct_refs`` fails; without
``attach_entry_ids`` the response has no ``entry_id`` at all.
"""

from __future__ import annotations

import re

from ai_agents.agents.expense_agent.categorize import DraftEntry
from backend.app.modules.policy.service import (
    CONTEXT_ENTRY_REF,
    _unique_entry_labels,
    attach_entry_ids,
)

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _draft(vendor: str | None, amount: float, date: str = "2026-06-10") -> DraftEntry:
    return DraftEntry(
        category="airfare",
        amount=amount,
        currency="USD",
        expense_date=date,
        vendor=vendor,
        description=None,
        status="draft",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


class TestUniqueEntryLabels:
    def test_distinct_vendors_keep_plain_labels(self) -> None:
        assert _unique_entry_labels([_draft("United Airlines", 320), _draft("Delta", 200)]) == [
            "United Airlines",
            "Delta",
        ]

    def test_duplicate_vendors_get_distinct_refs(self) -> None:
        labels = _unique_entry_labels(
            [_draft("United Airlines", 320), _draft("United Airlines", 450)]
        )
        assert labels == [
            "United Airlines (Airfare 320.00 USD on 2026-06-10)",
            "United Airlines (Airfare 450.00 USD on 2026-06-10)",
        ]

    def test_fully_identical_entries_get_ordinals(self) -> None:
        labels = _unique_entry_labels([_draft("United Airlines", 320)] * 2 + [_draft(None, 320)])
        assert len(set(labels)) == 3, labels
        assert labels[0].endswith("#1") and labels[1].endswith("#2")
        assert not any(_UUID_RE.search(x) for x in labels)

    def test_context_label_is_reserved(self) -> None:
        (label,) = _unique_entry_labels([_draft(CONTEXT_ENTRY_REF, 10)])
        assert label != CONTEXT_ENTRY_REF


class TestAttachEntryIds:
    def test_flags_and_items_carry_entry_id(self) -> None:
        units = [
            {"entry_ref": "United Airlines (Airfare 320.00 USD on 2026-06-10)", "entry_id": "a"},
            {"entry_ref": "United Airlines (Airfare 450.00 USD on 2026-06-10)", "entry_id": "b"},
            {"entry_ref": CONTEXT_ENTRY_REF},
        ]
        output = {
            "items": [{"entry_ref": u["entry_ref"]} for u in units],
            "flags": [
                {"entry_ref": units[1]["entry_ref"]},
                {"entry_ref": units[0]["entry_ref"]},
                {"entry_ref": CONTEXT_ENTRY_REF},
            ],
        }
        attach_entry_ids(output, units)
        assert [i["entry_id"] for i in output["items"]] == ["a", "b", None]
        assert [f["entry_id"] for f in output["flags"]] == ["b", "a", None]

    def test_malformed_output_is_left_alone(self) -> None:
        output: dict = {"items": "x", "flags": None}
        attach_entry_ids(output, [{"entry_ref": "r", "entry_id": "a"}])
        assert output == {"items": "x", "flags": None}
