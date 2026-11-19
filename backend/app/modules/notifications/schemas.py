"""Notification DTOs (product gap 3 — app-shell bell)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

NotificationKind = Literal[
    "approval_pending",  # approver: an approval is waiting on you
    "approval_escalated",  # finance: an approval was escalated for human review
    "approved",  # owner: your report / trip was approved (incl. auto-approved)
    "rejected",  # owner: your report / trip was rejected
    "returned",  # owner: your report / trip was returned for changes
    "reimbursed",  # owner: your report was reimbursed
    "completed",  # owner: your trip was completed
]

SubjectType = Literal["expense_report", "trip", "approval"]


class NotificationItem(BaseModel):
    """One derived notification.

    ``id`` is deterministic (derived from the source row, e.g.
    ``decision:<approval_step id>``) so the same event always has the same id and
    can be marked read.  ``link`` is the in-app route that opens the subject.
    """

    id: str
    kind: NotificationKind
    title: str
    body: str
    subject_type: SubjectType
    subject_id: uuid.UUID
    link: str
    created_at: datetime
    read: bool


class UnreadCountResponse(BaseModel):
    unread: int


class MarkReadResponse(BaseModel):
    marked: int
