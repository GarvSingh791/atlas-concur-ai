"""ORM foundation: the shared ``Base``, the composable convention mixins, and
the concrete models defined so far.

Identity models (P2-T3): Organization, User, Role, Permission, RolePermission.
AuditLog (P2-T2) is also re-exported so Alembic's env.py sees every table via
a single import.
Travel models (P2-T6): Trip, TripStatus, TripSegment, SegmentType.
Expense models (P2-T7): ExpenseReport, ExpenseReportStatus, Expense,
  ExpenseCategory, ExpenseStatus, ExpenseMatchSource.
Policy models (P3-T1): Policy, PolicyRule, PolicyEmbedding (+ enums).
Workflow models (P3-T1): CardTransaction, CalendarEvent, Receipt,
  Approval, ApprovalStep, Conversation, ConversationMessage, AiDecisionLog.
Integrity models (P3-T4): IdempotencyKey.
"""

from __future__ import annotations

from database.models.ai_decision_log import AiAgent, AiDecisionLog, AiRoute, ReflectionOutcome
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.auth_token import AuthToken, AuthTokenPurpose
from database.models.base import NAMING_CONVENTION, Base
from database.models.calendar_event import CalendarEvent
from database.models.card_transaction import CardTransaction
from database.models.conversation import Conversation, ConversationContextType
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.idempotency_key import IdempotencyKey
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.mixins import (
    AuthorshipMixin,
    CreatedAtMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)
from database.models.notification_read import NotificationRead
from database.models.policy import Policy
from database.models.policy_embedding import PolicyEmbedding
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

__all__ = [
    "NAMING_CONVENTION",
    "AiAgent",
    "AiDecisionLog",
    "AiRoute",
    "Approval",
    "ApprovalDecision",
    "ApprovalStatus",
    "ApprovalStep",
    "ApprovalSubjectType",
    "AuditLog",
    "AuthToken",
    "AuthTokenPurpose",
    "AuthorshipMixin",
    "Base",
    "CalendarEvent",
    "CardTransaction",
    "Conversation",
    "ConversationContextType",
    "ConversationMessage",
    "CreatedAtMixin",
    "Expense",
    "ExpenseCategory",
    "ExpenseMatchSource",
    "ExpenseReport",
    "ExpenseReportStatus",
    "ExpenseStatus",
    "IdempotencyKey",
    "MessageRole",
    "NotificationRead",
    "OcrStatus",
    "Organization",
    "Permission",
    "Policy",
    "PolicyAppliesTo",
    "PolicyEmbedding",
    "PolicyRule",
    "PolicyRuleStatus",
    "PolicyRuleType",
    "ProcessingPath",
    "Receipt",
    "ReflectionOutcome",
    "Role",
    "RoleKey",
    "RolePermission",
    "SegmentType",
    "SoftDeleteMixin",
    "TenantMixin",
    "TimestampMixin",
    "Trip",
    "TripSegment",
    "TripStatus",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserStatus",
    "VersionMixin",
]
