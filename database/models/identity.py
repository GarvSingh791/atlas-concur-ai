"""Identity domain models: Organization, User, Role, Permission, RolePermission.

Per DATABASE_SCHEMA.md (Core Entities).  Build order resolves FKs:
  organizations → users (org FK + self-ref manager_id) → roles → permissions
  → role_permissions (composite PK).

Deviation from schema doc (recorded in P2-T3 status report):
  users.password_hash TEXT NOT NULL — DATABASE_SCHEMA.md has no credential
  column, but P2-T3 mandates password hashing + login.  Smallest safe addition.
"""

from __future__ import annotations

import enum
import re
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.constants.grades import GRADE_CODES
from database.models.base import Base
from database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class RoleKey(str, enum.Enum):
    """Coarse role — shared by users.role (PG type user_role) and roles.key
    (PG type role_key).  String enum so values serialise cleanly."""

    employee = "employee"
    manager = "manager"
    finance_admin = "finance_admin"


class UserStatus(str, enum.Enum):
    """Life-cycle status for a user account (PG type user_status)."""

    active = "active"
    inactive = "inactive"


# ---------------------------------------------------------------------------
# Organization login code (``organizations.slug``, migration 0022)
# ---------------------------------------------------------------------------
#
# A stable, human-typable, NON-secret identifier a user types on the login page
# to say which organization they belong to.  Email is unique per organization
# only, so the same address may exist in two tenants; the slug disambiguates.

ORG_SLUG_MAX_LEN = 63
# Lowercase alphanumerics separated by single hyphens (no leading/trailing hyphen).
ORG_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ORG_SLUG_SQL_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"
# Words that are never used as a whole slug (they read like system pages or roles,
# and would invite phishing-style confusion).  A name that slugifies to one of
# them gets ``-org`` appended.
RESERVED_ORG_SLUGS = frozenset(
    {
        "admin",
        "administrator",
        "api",
        "app",
        "atlas",
        "auth",
        "default",
        "help",
        "internal",
        "login",
        "logout",
        "null",
        "none",
        "org",
        "organization",
        "organisation",
        "public",
        "root",
        "security",
        "settings",
        "signin",
        "signup",
        "static",
        "status",
        "support",
        "system",
        "undefined",
        "www",
    }
)
# Room for a "-xxxxxx" uniqueness suffix inside ORG_SLUG_MAX_LEN.
_ORG_SLUG_BASE_MAX = 48


def org_slug_base(name: str | None) -> str:
    """Deterministic slug stem from an organization name.

    Lower-cased, runs of anything outside ``[a-z0-9]`` collapsed to one hyphen,
    trimmed, cut to 48 chars.  Empty -> ``"org"``-prefixed fallback; a reserved
    word or a one-character stem gets ``-org`` appended.  Never returns a
    reserved word or anything shorter than 2 chars.
    """
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    base = base[:_ORG_SLUG_BASE_MAX].strip("-")
    if not base:
        return "org-unnamed"
    if base in RESERVED_ORG_SLUGS:
        return f"{base}-org"
    if len(base) < 2:  # keep the stem valid on its own (CHECK: 2..63 chars)
        return f"{base}-org"
    return base


def new_org_slug(name: str | None) -> str:
    """Slug for an organization created by application code: the name stem plus
    a short random suffix, so two tenants with the same name never collide."""
    return f"{org_slug_base(name)}-{secrets.token_hex(3)}"


def normalise_org_slug(raw: str | None) -> str | None:
    """What a user typed on the login page -> lookup key (None when blank)."""
    if raw is None:
        return None
    value = raw.strip().lower()
    return value or None


def _default_org_slug(context: Any) -> str:
    return new_org_slug(context.get_current_parameters().get("name"))


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tenant root — single row in the build; enables multi-tenancy later.

    No TenantMixin (it *is* the tenant) and no AuthorshipMixin (bootstrapped
    outside normal user-auth flow).
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    # ISO 4217 three-character currency code
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)

    # Organization login code (migration 0022).  ORM inserts get a name-derived
    # slug with a random suffix; raw SQL inserts fall back to the server default.
    slug: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=_default_org_slug,
        server_default=text("'org-' || substr(md5(gen_random_uuid()::text), 1, 10)"),
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        CheckConstraint(
            f"slug ~ '{ORG_SLUG_SQL_PATTERN}' AND char_length(slug) BETWEEN 2 AND "
            f"{ORG_SLUG_MAX_LEN}",
            name="slug_format",
        ),
    )

    users: Mapped[list[User]] = relationship("User", back_populates="organization")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Person in the system.  Three roles, self-referential manager_id,
    optional traveler preferences JSONB, and a stored password hash.

    Unique login identity: one email per organization, case-insensitive
    (uq_users_org_email_lower, migration 0022; the older case-sensitive
    uq_users_organization_id_email is kept).  The same email MAY exist in two
    organizations: login takes an optional organization slug to disambiguate.
    """

    __tablename__ = "users"

    # Tenant seam (real FK now that organizations exists)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)

    # Coarse role — PG enum type name "user_role"
    role: Mapped[RoleKey] = mapped_column(
        SAEnum(RoleKey, name="user_role", create_type=True),
        nullable=False,
    )

    department: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Self-referential: NULL for top-level users (manager_id points to manager)
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Life-cycle status — PG enum type name "user_status"
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status", create_type=True),
        nullable=False,
        server_default="active",
    )

    preferences: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Deviation from DATABASE_SCHEMA.md — required for password-based auth.
    # Never returned in responses; never logged.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # Traveller grade (AIR-04, migration 0021): a code from config/constants/grades.py,
    # set by a finance admin.  NULL = unknown — grade-based exceptions are then never
    # granted automatically (fail closed).
    grade: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Account recovery (migration 0024).  NULL = not verified (legacy / admin-
    # created accounts): the user keeps working, the UI shows a banner.  Set by
    # verifying the email, accepting an invite or completing a password reset.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # When the password last changed (reset / change / invite acceptance).  Every
    # access / refresh token carries a fingerprint of this value; a token minted
    # before the change no longer matches and is refused (fail closed).
    password_changed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        # Explicit name: the uq convention only captures the first column.
        UniqueConstraint("organization_id", "email", name="uq_users_organization_id_email"),
        Index("ix_users_manager_id", "manager_id"),
        # One account per (organization, lower(email)) — migration 0022 replaced
        # the global uq_users_email_lower of 0020 (organization-aware login).
        Index(
            "uq_users_org_email_lower",
            "organization_id",
            text("lower(email)"),
            unique=True,
        ),
        CheckConstraint(
            "grade IS NULL OR grade IN (" + ", ".join(f"'{c}'" for c in GRADE_CODES) + ")",
            name="grade_on_ladder",
        ),
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="users")
    manager: Mapped[User | None] = relationship(
        "User",
        remote_side="User.id",
        foreign_keys=[manager_id],
        back_populates="reports",
    )
    reports: Mapped[list[User]] = relationship(
        "User",
        back_populates="manager",
        foreign_keys=[manager_id],
    )


class Role(UUIDPrimaryKeyMixin, Base):
    """Global role reference row — one row per RoleKey value.

    No tenant (global reference data); no timestamps (immutable reference).
    """

    __tablename__ = "roles"

    # PG enum type name "role_key" (distinct from "user_role" on users.role)
    key: Mapped[RoleKey] = mapped_column(
        SAEnum(RoleKey, name="role_key", create_type=True),
        nullable=False,
        unique=True,
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    permissions: Mapped[list[Permission]] = relationship(
        "Permission", secondary="role_permissions", back_populates="roles"
    )


class Permission(UUIDPrimaryKeyMixin, Base):
    """Fine-grained permission key, e.g. 'expense.read.own'.

    No tenant (global); no timestamps (immutable reference).
    """

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    roles: Mapped[list[Role]] = relationship(
        "Role", secondary="role_permissions", back_populates="permissions"
    )


class RolePermission(Base):
    """M:M junction between roles and permissions.

    Composite PK prevents duplicates.  No UUIDPrimaryKeyMixin, no timestamps.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint("role_id", "permission_id"),
        Index("ix_role_permissions_permission_id", "permission_id"),
    )
