"""Public-demo protections (phase 11) — DEMO_MODE.

The public demo shares five well-known accounts (the seeded ``@atlas-demo.internal``
users of the "Atlas Demo Corp" organisation, password published in the README).
With ``DEMO_MODE=true``:

* those five PROTECTED DEMO ACCOUNTS cannot be deactivated or edited (name, role,
  manager, department, grade) by anyone — finance admins included.  Email, password
  and organisation have no write endpoint at all.  Refusals are 403
  ``DEMO_ACCOUNT_PROTECTED`` and audited.  Users created by visitors stay fully
  manageable;
* (with ``DEMO_RESTRICT_DESTRUCTIVE``, which follows DEMO_MODE unless set) the seeded
  policy rules cannot be archived — archiving is terminal, so one visitor could
  otherwise switch the policy engine off for everyone.  Activating, authoring new
  rules and archiving visitor-authored rules all still work.

Identification never trusts the client: an account is protected only when its stored
email is one of the seed's ``DEMO_GRADES`` emails AND its stored organisation is the
one named exactly ``DEMO_ORG_NAME``.  A seeded rule is identified by PROVENANCE
(:func:`seeded_rule_ids`, shared with the demo reset): a rule of that organisation's
stub policy (seed name + version), with a (clause, type, target) the seed creates,
that was NOT authored through the API.  Every rule the API creates
(``POST /policies/{id}/rules``) gets a ``policy_rule.drafted`` audit row in the same
transaction; ``audit_log`` is append-only and that row outlives its author (the
actor FK only goes NULL), so a visitor's lookalike that copies a seeded rule's
content stays archivable.  ``created_by`` alone cannot decide it: the seed records
the demo finance admin as the author — the very account visitors share.  Every
check runs server-side; the UI lock badge is only a hint.

The seed modules are imported lazily (they pull the corpus and the seeding stack,
which the request path does not otherwise need).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from functools import cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.errors import AppError
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.identity import Organization, User
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule

DEMO_ACCOUNT_PROTECTED = "DEMO_ACCOUNT_PROTECTED"
DEMO_RULE_PROTECTED = "DEMO_RULE_PROTECTED"

DEMO_ACCOUNT_MESSAGE = (
    "This is a shared demo account, so it can't be changed or deactivated. "
    "Create your own user to try user administration."
)
DEMO_RULE_MESSAGE = (
    "This rule is part of the shared demo policy, so it can't be archived. "
    "Author a new rule to try archiving."
)


class DemoProtectedError(AppError):
    status_code = 403
    code = DEMO_ACCOUNT_PROTECTED


def demo_mode() -> bool:
    return bool(get_settings().DEMO_MODE)


def restrict_destructive() -> bool:
    """DEMO_RESTRICT_DESTRUCTIVE, defaulting to on whenever DEMO_MODE is on."""
    s = get_settings()
    if not s.DEMO_MODE:
        return False
    return True if s.DEMO_RESTRICT_DESTRUCTIVE is None else bool(s.DEMO_RESTRICT_DESTRUCTIVE)


@cache
def demo_org_name() -> str:
    from database.seeds.seed_demo_grades import DEMO_ORG_NAME  # noqa: PLC0415

    return DEMO_ORG_NAME


@cache
def protected_demo_emails() -> frozenset[str]:
    """The five seeded demo accounts (the seed's DEMO_GRADES keys)."""
    from database.seeds.seed_all import DEMO_GRADES  # noqa: PLC0415

    return frozenset(email.lower() for email in DEMO_GRADES)


@cache
def seeded_rule_keys() -> frozenset[tuple[str, str, str]]:
    """(source_clause_ref, rule_type, applies_to) of every rule the seed creates."""
    from database.seeds.policy_content import (  # noqa: PLC0415
        ENTRY_RULES,
        PROCESS_RULES,
        RULES,
    )
    from database.seeds.seed_all import CAR_CAP_DRAFT_RULE  # noqa: PLC0415

    return frozenset(
        (str(r["clause_ref"]), str(r["rule_type"]), str(r["applies_to"]))
        for r in (*RULES, *ENTRY_RULES, *PROCESS_RULES, CAR_CAP_DRAFT_RULE)
    )


def _demo_org_ids(session: Session, org_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = set(org_ids)
    if not ids:
        return set()
    return set(
        session.scalars(
            sa.select(Organization.id).where(
                Organization.id.in_(ids), Organization.name == demo_org_name()
            )
        ).all()
    )


def protected_user_ids(session: Session, users: Iterable[User]) -> set[uuid.UUID]:
    """Ids of the given users that are protected demo accounts (empty when off)."""
    if not demo_mode():
        return set()
    emails = protected_demo_emails()
    candidates = [u for u in users if (u.email or "").lower() in emails]
    demo_orgs = _demo_org_ids(session, {u.organization_id for u in candidates})
    return {u.id for u in candidates if u.organization_id in demo_orgs}


def is_protected_account(session: Session, user: User) -> bool:
    return user.id in protected_user_ids(session, [user])


def _refuse(
    session: Session,
    *,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    operation: str,
    code: str,
    message: str,
) -> DemoProtectedError:
    """Audit the refused attempt (committed — the request transaction is otherwise
    discarded; nothing else is pending at the call sites) and return the 403."""
    record_audit(
        session,
        organization_id=organization_id,
        action=f"{entity_type}.demo_protected_refused",
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        before_state=None,
        after_state={"reason": code, "operation": operation},
    )
    session.commit()
    return DemoProtectedError(message, code=code)


def guard_protected_account(session: Session, *, admin: User, target: User, operation: str) -> None:
    """Refuse (403 DEMO_ACCOUNT_PROTECTED, audited) any change to a protected account."""
    if is_protected_account(session, target):
        raise _refuse(
            session,
            organization_id=admin.organization_id,
            actor_id=admin.id,
            entity_type="user",
            entity_id=target.id,
            operation=operation,
            code=DEMO_ACCOUNT_PROTECTED,
            message=DEMO_ACCOUNT_MESSAGE,
        )


# The audit action POST /policies/{id}/rules writes for every rule it creates.
API_AUTHORED_RULE_ACTION = "policy_rule.drafted"


def rule_seed_key(rule: PolicyRule) -> tuple[str, str, str]:
    return (rule.source_clause_ref or "", rule.rule_type.value, rule.applies_to.value)


def api_authored_rule_ids(session: Session, rule_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Ids among ``rule_ids`` that were authored through the API (provenance)."""
    ids = set(rule_ids)
    if not ids:
        return set()
    return set(
        session.scalars(
            sa.select(AuditLog.entity_id).where(
                AuditLog.entity_type == "policy_rule",
                AuditLog.action == API_AUTHORED_RULE_ACTION,
                AuditLog.entity_id.in_(ids),
            )
        ).all()
    )


def seeded_rule_ids(
    session: Session, *, policy: Policy, rules: Iterable[PolicyRule]
) -> set[uuid.UUID]:
    """The seeded rules among ``rules`` of the stub ``policy`` (provenance test).

    Shared by the archive guard and ``database/seeds/reset_demo_data.py`` so both
    agree on what is seeded.  Does NOT check the organisation (the reset already
    works on the demo org only; :func:`is_seeded_rule` adds the check).
    """
    from database.seeds.policy_content import POLICY_NAME, POLICY_VERSION  # noqa: PLC0415

    if policy.name != POLICY_NAME or policy.version != POLICY_VERSION:
        return set()
    keys = seeded_rule_keys()
    candidates = [r for r in rules if r.policy_id == policy.id and rule_seed_key(r) in keys]
    authored = api_authored_rule_ids(session, (r.id for r in candidates))
    return {r.id for r in candidates if r.id not in authored}


def is_seeded_rule(session: Session, *, policy: Policy, rule: PolicyRule) -> bool:
    if rule.id not in seeded_rule_ids(session, policy=policy, rules=[rule]):
        return False
    return bool(_demo_org_ids(session, [policy.organization_id]))


def guard_seeded_rule_archive(
    session: Session, *, policy: Policy, rule: PolicyRule, actor_id: uuid.UUID, target: Any
) -> None:
    """Refuse (403 DEMO_RULE_PROTECTED, audited) archiving a seeded demo rule."""
    if str(getattr(target, "value", target)) != "archived" or not restrict_destructive():
        return
    if is_seeded_rule(session, policy=policy, rule=rule):
        raise _refuse(
            session,
            organization_id=policy.organization_id,
            actor_id=actor_id,
            entity_type="policy_rule",
            entity_id=rule.id,
            operation="archive",
            code=DEMO_RULE_PROTECTED,
            message=DEMO_RULE_MESSAGE,
        )
