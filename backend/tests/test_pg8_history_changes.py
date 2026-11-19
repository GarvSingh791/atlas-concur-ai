"""Product gap 8 — review fix: the per-rule history reports only REAL changes (hermetic).

An activation / archive audit row carries context fields (title, rule_type, applies_to)
in ``after_state`` only; those did not change and must not be listed as "— → value".
A creation event (no rule content before) lists every field; a removal event (no rule
content after) lists every field it removed.
"""

from __future__ import annotations

from backend.app.modules.policy.history import field_changes


def _plain(changes: list) -> list[dict]:
    return [c.model_dump() for c in changes]


def test_activation_lists_only_the_status_change() -> None:
    before = {"status": "draft"}
    after = {
        "status": "active",
        "rule_type": "amount_cap",
        "applies_to": "all",
        "title": "Hotel cap",
        "summary": ["..."],
    }
    assert _plain(field_changes(before, after)) == [
        {"field": "status", "before": "draft", "after": "active"}
    ]


def test_archive_lists_only_the_status_change() -> None:
    after = {"status": "archived", "rule_type": "per_diem", "applies_to": "meals", "title": "x"}
    assert _plain(field_changes({"status": "active"}, after)) == [
        {"field": "status", "before": "active", "after": "archived"}
    ]


def test_creation_lists_every_field() -> None:
    after = {"status": "draft", "rule_type": "amount_cap", "title": "Cap", "intent": "secret"}
    assert _plain(field_changes(None, after)) == [
        {"field": "status", "before": None, "after": "draft"},
        {"field": "title", "before": None, "after": "Cap"},
        {"field": "rule_type", "before": None, "after": "amount_cap"},
    ]


def test_removal_lists_what_was_removed() -> None:
    before = {"status": "active", "source_clause_ref": "GRND-05", "definition": {"k": 1}}
    changes = _plain(field_changes(before, {"source": "migration"}))
    assert {"field": "status", "before": "active", "after": None} in changes
    assert {"field": "source_clause_ref", "before": "GRND-05", "after": None} in changes
    assert {"field": "definition.k", "before": 1, "after": None} in changes


def test_definition_update_lists_changed_keys_only() -> None:
    changes = field_changes(
        {"definition": {"a": 1, "b": 2}}, {"definition": {"a": 1, "b": 3}, "title": "ctx"}
    )
    assert _plain(changes) == [{"field": "definition.b", "before": 2, "after": 3}]
