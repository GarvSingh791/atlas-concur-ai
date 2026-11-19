"""Follow-up (grades): the approver-grade requirement of a flag set (hermetic)."""

from __future__ import annotations

import pytest

from backend.app.modules.approval.grade_routing import required_grade
from config.constants import grades as G


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("SVP", "SVP"),
        ("VP", "VP"),
        ("Vice President", "VP"),
        ("C-suite", "CSUITE"),
        (" svp ", "SVP"),
        ("manager", None),  # the line manager: a reporting line, not the Manager grade
        ("Manager", None),
        ("Legal/Compliance", None),
        ("", None),
        (None, None),
    ],
)
def test_approver_grade(text: str | None, code: str | None) -> None:
    assert G.approver_grade(text) == code


def test_most_senior_wins_and_names_its_clauses() -> None:
    flags = [
        {"requires_approver": "manager", "source_clause_ref": "PTA-02"},
        {"requires_approver": "VP", "source_clause_ref": "AIR-CABIN"},
        {"requires_approver": "SVP", "source_clause_ref": "NR-01"},
        {"requires_approver": "SVP", "source_clause_ref": "AIR-07"},
        {"requires_approver": None},
        "not a flag",
    ]
    assert required_grade(flags) == ("SVP", ["AIR-07", "NR-01"])


def test_vp_only() -> None:
    assert required_grade([{"requires_approver": "VP", "source_clause_ref": "EXP-01"}]) == (
        "VP",
        ["EXP-01"],
    )


@pytest.mark.parametrize(
    "flags",
    [None, [], [{"requires_approver": "manager"}], [{"requires_approver": "Legal/Compliance"}]],
)
def test_no_grade_required(flags: list | None) -> None:
    assert required_grade(flags) == (None, [])


def test_advisory_note_with_a_required_grade_in_observed_requires_nothing() -> None:
    # an SVP's in-policy AIR-07 note names the threshold but needs no approver
    note = {
        "flag_type": "policy_advisory",
        "requires_approver": None,
        "observed": {"required_grade": "SVP"},
    }
    assert required_grade([note]) == (None, [])


def test_a_foreign_org_user_is_never_eligible() -> None:
    # Defence in depth behind the chain walk's tenant stop (review fix).
    import uuid
    from types import SimpleNamespace

    from backend.app.modules.approval.grade_routing import _eligible
    from database.models.identity import RoleKey, UserStatus

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    approval = SimpleNamespace(organization_id=org_a, submitter_id=uuid.uuid4())

    def user(org: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            organization_id=org,
            status=UserStatus.active,
            role=RoleKey.manager,
            grade="CSUITE",
        )

    assert _eligible(user(org_a), approval=approval, need="SVP") is True  # type: ignore[arg-type]
    assert _eligible(user(org_b), approval=approval, need="SVP") is False  # type: ignore[arg-type]
