"""Test factories for Atlas backend.

One-liner builders for every ORM entity. Each builder:
  - fills all NOT-NULL / no-server-default columns with schema-faithful defaults
  - auto-creates parent FKs when not supplied
  - calls session.flush() so server-generated UUID PKs are populated
  - returns the ORM instance

Usage::

    org = make_org(db_session)
    user = make_user(db_session, org=org, role=RoleKey.manager)
    employee = make_user(db_session, org=org, role=RoleKey.employee, manager_id=user.id)
"""

from __future__ import annotations

import itertools
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.app.core.security import hash_password
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User, UserStatus
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

# Bcrypt at 12 rounds is slow — compute ONE hash at module import time and reuse.
# Tests can override via the password_hash kwarg.
_DEFAULT_PASSWORD = "test-password-Atlas1!"  # noqa: S105
_DEFAULT_PW_HASH: str = hash_password(_DEFAULT_PASSWORD)

_counter = itertools.count(1)


def _n() -> int:
    return next(_counter)


# ---------------------------------------------------------------------------
# Organization
# ---------------------------------------------------------------------------


def make_org(
    session: Session,
    *,
    name: str | None = None,
    base_currency: str = "USD",
    **kwargs: object,
) -> Organization:
    org = Organization(
        name=name or f"Org-{_n()}",
        base_currency=base_currency,
        **kwargs,
    )
    session.add(org)
    session.flush()
    return org


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


def make_user(
    session: Session,
    *,
    org: Organization | None = None,
    name: str | None = None,
    email: str | None = None,
    role: RoleKey = RoleKey.employee,
    manager_id: uuid.UUID | None = None,
    status: UserStatus = UserStatus.active,
    password_hash: str | None = None,
    **kwargs: object,
) -> User:
    if org is None:
        org = make_org(session)
    n = _n()
    user = User(
        organization_id=org.id,
        name=name or f"User {n}",
        email=email or f"user{n}@test.atlas.internal",
        role=role,
        manager_id=manager_id,
        status=status,
        password_hash=password_hash or _DEFAULT_PW_HASH,
        **kwargs,
    )
    session.add(user)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Policy + PolicyRule
# ---------------------------------------------------------------------------


def make_policy(
    session: Session,
    *,
    org: Organization | None = None,
    name: str | None = None,
    version: int = 1,
    effective_from: date | None = None,
    **kwargs: object,
) -> Policy:
    if org is None:
        org = make_org(session)
    policy = Policy(
        organization_id=org.id,
        name=name or f"Policy-{_n()}",
        version=version,
        effective_from=effective_from or date.today(),
        **kwargs,
    )
    session.add(policy)
    session.flush()
    return policy


def make_policy_rule(
    session: Session,
    *,
    policy: Policy | None = None,
    rule_type: PolicyRuleType = PolicyRuleType.amount_cap,
    applies_to: PolicyAppliesTo = PolicyAppliesTo.all,
    definition: dict | None = None,
    status: PolicyRuleStatus = PolicyRuleStatus.active,
    **kwargs: object,
) -> PolicyRule:
    if policy is None:
        policy = make_policy(session)
    rule = PolicyRule(
        policy_id=policy.id,
        rule_type=rule_type,
        applies_to=applies_to,
        definition=definition if definition is not None else {"max_amount": 500},
        status=status,
        **kwargs,
    )
    session.add(rule)
    session.flush()
    return rule


# ---------------------------------------------------------------------------
# Trip + TripSegment
# ---------------------------------------------------------------------------


def make_trip(
    session: Session,
    *,
    org: Organization | None = None,
    traveler: User | None = None,
    origin: str = "NYC",
    primary_destination: str = "SFO",
    start_date: date | None = None,
    end_date: date | None = None,
    currency: str = "USD",
    status: TripStatus = TripStatus.draft,
    **kwargs: object,
) -> Trip:
    if org is None and traveler is None:
        org = make_org(session)
    if traveler is None:
        traveler = make_user(session, org=org)
    _start = start_date or date.today()
    _end = end_date or (_start + timedelta(days=3))
    trip = Trip(
        organization_id=traveler.organization_id,
        traveler_id=traveler.id,
        origin=origin,
        primary_destination=primary_destination,
        start_date=_start,
        end_date=_end,
        currency=currency,
        status=status,
        **kwargs,
    )
    session.add(trip)
    session.flush()
    return trip


def make_trip_segment(
    session: Session,
    *,
    trip: Trip | None = None,
    segment_type: SegmentType = SegmentType.flight,
    **kwargs: object,
) -> TripSegment:
    if trip is None:
        trip = make_trip(session)
    seg = TripSegment(
        trip_id=trip.id,
        segment_type=segment_type,
        **kwargs,
    )
    session.add(seg)
    session.flush()
    return seg


# ---------------------------------------------------------------------------
# ExpenseReport + Expense
# ---------------------------------------------------------------------------


def make_expense_report(
    session: Session,
    *,
    org: Organization | None = None,
    owner: User | None = None,
    title: str = "Test Expense Report",
    currency: str = "USD",
    status: ExpenseReportStatus = ExpenseReportStatus.draft,
    **kwargs: object,
) -> ExpenseReport:
    if org is None and owner is None:
        org = make_org(session)
    if owner is None:
        owner = make_user(session, org=org)
    report = ExpenseReport(
        organization_id=owner.organization_id,
        owner_id=owner.id,
        title=title,
        currency=currency,
        status=status,
        **kwargs,
    )
    session.add(report)
    session.flush()
    return report


def make_expense(
    session: Session,
    *,
    org: Organization | None = None,
    report: ExpenseReport | None = None,
    category: ExpenseCategory = ExpenseCategory.meals,
    amount: Decimal | str = "25.00",
    currency: str = "USD",
    expense_date: date | None = None,
    status: ExpenseStatus = ExpenseStatus.draft,
    **kwargs: object,
) -> Expense:
    if report is None:
        report = make_expense_report(session, org=org)
    expense = Expense(
        organization_id=report.organization_id,
        report_id=report.id,
        category=category,
        amount=Decimal(str(amount)),
        currency=currency,
        expense_date=expense_date or date.today(),
        status=status,
        **kwargs,
    )
    session.add(expense)
    session.flush()
    return expense


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def make_receipt(
    session: Session,
    *,
    org: Organization | None = None,
    uploaded_by: User | None = None,
    image_url: str = "s3://atlas-test-bucket/receipts/test.jpg",
    ocr_status: OcrStatus = OcrStatus.pending,
    **kwargs: object,
) -> Receipt:
    if org is None and uploaded_by is None:
        org = make_org(session)
    if uploaded_by is None:
        uploaded_by = make_user(session, org=org)
    receipt = Receipt(
        organization_id=uploaded_by.organization_id,
        uploaded_by=uploaded_by.id,
        image_url=image_url,
        ocr_status=ocr_status,
        **kwargs,
    )
    session.add(receipt)
    session.flush()
    return receipt


# ---------------------------------------------------------------------------
# CardTransaction
# ---------------------------------------------------------------------------


def make_card_transaction(
    session: Session,
    *,
    org: Organization | None = None,
    user: User | None = None,
    amount: Decimal | str = "42.00",
    currency: str = "USD",
    txn_date: date | None = None,
    **kwargs: object,
) -> CardTransaction:
    if org is None and user is None:
        org = make_org(session)
    if user is None:
        user = make_user(session, org=org)
    txn = CardTransaction(
        organization_id=user.organization_id,
        user_id=user.id,
        amount=Decimal(str(amount)),
        currency=currency,
        txn_date=txn_date or date.today(),
        **kwargs,
    )
    session.add(txn)
    session.flush()
    return txn


# ---------------------------------------------------------------------------
# Approval + ApprovalStep
# ---------------------------------------------------------------------------


def make_approval(
    session: Session,
    *,
    org: Organization | None = None,
    submitter: User | None = None,
    subject_type: ApprovalSubjectType = ApprovalSubjectType.expense_report,
    subject_id: uuid.UUID | None = None,
    current_status: ApprovalStatus = ApprovalStatus.pending,
    **kwargs: object,
) -> Approval:
    if org is None and submitter is None:
        org = make_org(session)
    if submitter is None:
        submitter = make_user(session, org=org)
    approval = Approval(
        organization_id=submitter.organization_id,
        subject_type=subject_type,
        subject_id=subject_id or uuid.uuid4(),
        submitter_id=submitter.id,
        current_status=current_status,
        **kwargs,
    )
    session.add(approval)
    session.flush()
    return approval


def make_approval_step(
    session: Session,
    *,
    approval: Approval | None = None,
    step_order: int = 1,
    decision: ApprovalDecision = ApprovalDecision.approve,
    **kwargs: object,
) -> ApprovalStep:
    if approval is None:
        approval = make_approval(session)
    step = ApprovalStep(
        approval_id=approval.id,
        step_order=step_order,
        decision=decision,
        **kwargs,
    )
    session.add(step)
    session.flush()
    return step
