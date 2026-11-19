"""P7-T1 · item B′ — backend policy service (org-scoping JOIN + rule lifecycle).

Targets ``backend.app.modules.policy.service``: ``update_rule_status``,
``list_active_rules``, ``get_active_policy_for_org``, ``get_policy_for_org``.

The load-bearing invariant: ``policy_rules`` carries NO ``organization_id`` (it
inherits tenancy via ``policy_id`` → ``policies``). Org-scoping therefore relies
ENTIRELY on the explicit JOIN ``PolicyRule → Policy`` filtered on
``Policy.organization_id`` (service.py:106-113). A cross-org rule_id must resolve
to ``NotFoundError`` — never another tenant's row. These tests prove that JOIN
holds and that the rule-status lifecycle / enum-filter guards raise the right
errors. All are live-DB (inherited ``db_session``; self-skips if Postgres down).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.errors import AppError, NotFoundError, UnprocessableEntityError
from backend.app.modules.policy.service import (
    get_active_policy_for_org,
    get_policy_for_org,
    list_active_rules,
    update_rule_status,
)
from database.models.identity import Organization
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType

# ---------------------------------------------------------------------------
# update_rule_status — ORG-SCOPING JOIN proof (no cross-tenant leak)
# ---------------------------------------------------------------------------


def test_update_rule_status_cross_org_raises_not_found(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """A rule belonging to org A must be invisible to org B.

    policy_rules has no organization_id; the only tenant guard is the
    PolicyRule→Policy JOIN on Policy.organization_id. Passing org B's id for an
    org-A rule MUST raise NotFoundError (not transition the row, not leak it).

    Mutation target: dropping the ``Policy.organization_id == organization_id``
    filter in update_rule_status turns this 404 into a silent cross-tenant write.
    """
    policy_a = f.make_policy(db_session, org=org_a)
    rule_a = f.make_policy_rule(db_session, policy=policy_a, status=PolicyRuleStatus.draft)

    with pytest.raises(NotFoundError):
        update_rule_status(
            db_session,
            rule_id=rule_a.id,
            organization_id=org_b.id,  # WRONG tenant
            new_status="active",
            actor_id=uuid.uuid4(),
        )

    # And the row is untouched — proves the cross-tenant call did not mutate it.
    db_session.refresh(rule_a)
    assert rule_a.status == PolicyRuleStatus.draft


def test_update_rule_status_unknown_rule_raises_not_found(
    db_session: Session, org_a: Organization
) -> None:
    with pytest.raises(NotFoundError):
        update_rule_status(
            db_session,
            rule_id=uuid.uuid4(),  # nonexistent
            organization_id=org_a.id,
            new_status="active",
            actor_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# update_rule_status — lifecycle transitions (_ALLOWED_TRANSITIONS)
# ---------------------------------------------------------------------------


def test_update_rule_status_draft_to_active_succeeds(
    db_session: Session, org_a: Organization
) -> None:
    policy = f.make_policy(db_session, org=org_a)
    # Issue 5: activation validates the definition — use the seeded CAR-CAP-DRAFT shape.
    rule = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.car,
        definition={"currency": "USD", "per": "day", "tiers": [{"tier": "standard", "cap": 75}]},
        status=PolicyRuleStatus.draft,
    )
    actor = f.make_user(db_session, org=org_a)

    ret_policy, ret_rule, previous = update_rule_status(
        db_session,
        rule_id=rule.id,
        organization_id=org_a.id,
        new_status="active",
        actor_id=actor.id,
    )

    assert previous == "draft"
    assert ret_rule.status == PolicyRuleStatus.active
    assert ret_policy.id == policy.id
    db_session.refresh(rule)
    assert rule.status == PolicyRuleStatus.active


def test_update_rule_status_refuses_to_activate_an_unenforceable_definition(
    db_session: Session, org_a: Organization
) -> None:
    """Issue 5: a definition the engine would silently mis-enforce is never activated."""
    policy = f.make_policy(db_session, org=org_a)
    rule = f.make_policy_rule(db_session, policy=policy, status=PolicyRuleStatus.draft)

    with pytest.raises(AppError) as exc_info:
        update_rule_status(
            db_session,
            rule_id=rule.id,
            organization_id=org_a.id,
            new_status="active",
            actor_id=uuid.uuid4(),
        )

    assert exc_info.value.code == "RULE_INVALID" and exc_info.value.status_code == 422
    db_session.refresh(rule)
    assert rule.status == PolicyRuleStatus.draft


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (PolicyRuleStatus.archived, "active"),  # archived is terminal
        (PolicyRuleStatus.archived, "draft"),
        (PolicyRuleStatus.active, "draft"),  # never silently revert to draft
    ],
)
def test_update_rule_status_illegal_transition_raises_422(
    db_session: Session,
    org_a: Organization,
    start: PolicyRuleStatus,
    target: str,
) -> None:
    """Illegal lifecycle transitions raise UnprocessableEntityError and leave
    the rule unchanged."""
    policy = f.make_policy(db_session, org=org_a)
    rule = f.make_policy_rule(db_session, policy=policy, status=start)

    with pytest.raises(UnprocessableEntityError):
        update_rule_status(
            db_session,
            rule_id=rule.id,
            organization_id=org_a.id,
            new_status=target,
            actor_id=uuid.uuid4(),
        )

    db_session.refresh(rule)
    assert rule.status == start  # no state change on the illegal path


def test_update_rule_status_invalid_status_string_raises_value_error(
    db_session: Session, org_a: Organization
) -> None:
    """An unrecognized status string fails at the PolicyRuleStatus(...) coercion
    (ValueError) before any DB work — guards against a bogus target leaking in."""
    policy = f.make_policy(db_session, org=org_a)
    rule = f.make_policy_rule(db_session, policy=policy, status=PolicyRuleStatus.draft)

    with pytest.raises(ValueError):
        update_rule_status(
            db_session,
            rule_id=rule.id,
            organization_id=org_a.id,
            new_status="not_a_real_status",
            actor_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# list_active_rules — org-scoped via policy; invalid enum filters → 422
# ---------------------------------------------------------------------------


def test_list_active_rules_returns_only_this_orgs_rules(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """list_active_rules resolves the active policy for the given org and returns
    only that policy's rules — never another tenant's rules."""
    policy_a = f.make_policy(db_session, org=org_a, name="Alpha Policy")
    rule_a = f.make_policy_rule(db_session, policy=policy_a)
    policy_b = f.make_policy(db_session, org=org_b, name="Beta Policy")
    rule_b = f.make_policy_rule(db_session, policy=policy_b)

    policy, rules = list_active_rules(db_session, organization_id=org_a.id)

    rule_ids = {r.id for r in rules}
    assert policy.id == policy_a.id
    assert rule_a.id in rule_ids
    assert rule_b.id not in rule_ids  # org B's rule never leaks into org A's list


def test_list_active_rules_invalid_status_filter_raises_422(
    db_session: Session, org_a: Organization
) -> None:
    f.make_policy(db_session, org=org_a)
    with pytest.raises(UnprocessableEntityError):
        list_active_rules(db_session, organization_id=org_a.id, status="bogus_status")


def test_list_active_rules_invalid_applies_to_filter_raises_422(
    db_session: Session, org_a: Organization
) -> None:
    f.make_policy(db_session, org=org_a)
    with pytest.raises(UnprocessableEntityError):
        list_active_rules(db_session, organization_id=org_a.id, applies_to="spaceship")


def test_list_active_rules_valid_filters_narrow_results(
    db_session: Session, org_a: Organization
) -> None:
    """A valid enum filter actually narrows — proves the filter is applied, not
    silently ignored."""
    policy = f.make_policy(db_session, org=org_a)
    flight_rule = f.make_policy_rule(db_session, policy=policy, applies_to=PolicyAppliesTo.flight)
    hotel_rule = f.make_policy_rule(db_session, policy=policy, applies_to=PolicyAppliesTo.hotel)

    _, rules = list_active_rules(db_session, organization_id=org_a.id, applies_to="flight")

    rule_ids = {r.id for r in rules}
    assert flight_rule.id in rule_ids
    assert hotel_rule.id not in rule_ids


# ---------------------------------------------------------------------------
# get_active_policy_for_org / get_policy_for_org — tenant-scoped, 404 on miss
# ---------------------------------------------------------------------------


def test_get_active_policy_for_org_raises_when_none_active(
    db_session: Session, org_a: Organization
) -> None:
    """An org with no active policy raises NotFoundError."""
    f.make_policy(db_session, org=org_a, is_active=False)
    with pytest.raises(NotFoundError):
        get_active_policy_for_org(db_session, organization_id=org_a.id)


def test_get_policy_for_org_cross_org_raises_not_found(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """A policy id from org A is a 404 when queried under org B (no leak)."""
    policy_a = f.make_policy(db_session, org=org_a)
    with pytest.raises(NotFoundError):
        get_policy_for_org(db_session, policy_id=policy_a.id, organization_id=org_b.id)
