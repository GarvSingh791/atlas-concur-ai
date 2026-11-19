"""Organization-aware login: organizations.slug, and email unique per organization.

Revision ID: 0022_org_scoped_email
Revises: 0021_traveller_grades
Create Date: 2026-11-14

Known-limits item "cross-organisation email clash".  0020 made ``users.email``
unique across ALL organizations because login resolved a user by email alone.
That let one tenant's admin claim another tenant's address first (squatting) and
told an admin that an address existed somewhere (enumeration).  Login now takes
an optional organization login code, so uniqueness goes back to per-organization.

upgrade (safe on a populated database — users/organizations are small tables):
  1. ``organizations.slug`` added NULLABLE, then backfilled for rows still NULL
     with a deterministic slug derived from the name (lower-case ``[a-z0-9-]``,
     reserved words get ``-org``, collisions get ``-2``, ``-3`` … in
     ``created_at, id`` order).  Then SET NOT NULL, a server default (random,
     for raw inserts), a unique constraint and a format check.
  2. The per-organization, case-insensitive unique index
     ``uq_users_org_email_lower (organization_id, lower(email))`` is created.
     Before that, case-insensitive duplicates inside one organization are
     counted; if any exist the migration FAILS CLOSED (count only, no email).
     They cannot exist after 0020, so this is a backstop.
  3. The global ``uq_users_email_lower`` from 0020 is dropped.

downgrade restores the global index only when that is safe: if any email now
exists in more than one organization it FAILS CLOSED (count only) instead of
guessing which account to delete.  Otherwise it recreates the global index, drops
the per-organization one, and drops the slug column with its constraints.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0022_org_scoped_email"
down_revision: str | None = "0021_traveller_grades"
branch_labels: str | None = None
depends_on: str | None = None


_GLOBAL_EMAIL_INDEX = "uq_users_email_lower"
_ORG_EMAIL_INDEX = "uq_users_org_email_lower"
_SLUG_UNIQUE = "uq_organizations_slug"
_SLUG_CHECK = "ck_organizations_slug_format"
_SLUG_SQL_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"
_SLUG_SERVER_DEFAULT = "'org-' || substr(md5(gen_random_uuid()::text), 1, 10)"

# Frozen copy of the slug rules (a migration must not change when app code does).
_RESERVED = frozenset(
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
_BASE_MAX = 48


def slug_base(name: str | None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    base = base[:_BASE_MAX].strip("-")
    if not base:
        return "org-unnamed"
    if base in _RESERVED:
        return f"{base}-org"
    if len(base) < 2:  # the CHECK needs 2..63 chars ("X Corp" ok, "X" not)
        return f"{base}-org"
    return base


def assign_slugs(rows: Iterable[tuple[object, str | None]], taken: set[str]) -> dict[object, str]:
    """Deterministic slugs for ``(id, name)`` rows given in a stable order.

    ``taken`` holds slugs already in use (rows that already have one keep it —
    re-running the backfill never changes an assigned slug).  Collisions get
    ``-2``, ``-3`` … appended.
    """
    used = set(taken)
    out: dict[object, str] = {}
    for row_id, name in rows:
        base = slug_base(name)
        candidate = base
        n = 1
        while candidate in used:
            n += 1
            candidate = f"{base}-{n}"
        used.add(candidate)
        out[row_id] = candidate
    return out


def _backfill_slugs(bind: sa.Connection) -> None:
    taken = {
        str(s)
        for s in bind.execute(
            sa.text("SELECT slug FROM organizations WHERE slug IS NOT NULL")
        ).scalars()
    }
    rows = bind.execute(
        sa.text("SELECT id, name FROM organizations WHERE slug IS NULL ORDER BY created_at, id")
    ).all()
    for row_id, slug in assign_slugs(((r[0], r[1]) for r in rows), taken).items():
        bind.execute(
            sa.text("UPDATE organizations SET slug = :slug WHERE id = :id AND slug IS NULL"),
            {"slug": slug, "id": row_id},
        )


def upgrade() -> None:
    bind = op.get_bind()

    # 1. organizations.slug: nullable -> backfill -> NOT NULL + constraints.
    op.add_column("organizations", sa.Column("slug", sa.Text(), nullable=True))
    _backfill_slugs(bind)
    op.alter_column(
        "organizations",
        "slug",
        nullable=False,
        server_default=sa.text(_SLUG_SERVER_DEFAULT),
    )
    op.create_unique_constraint(_SLUG_UNIQUE, "organizations", ["slug"])
    op.create_check_constraint(
        op.f(_SLUG_CHECK),
        "organizations",
        f"slug ~ '{_SLUG_SQL_PATTERN}' AND char_length(slug) BETWEEN 2 AND 63",
    )

    # 2. One account per (organization, lower(email)).
    duplicates = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM users "
            "GROUP BY organization_id, lower(email) HAVING count(*) > 1) AS d"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"0022: {duplicates} email address(es) are shared by more than one user "
            "inside the same organization (case-insensitive). Resolve them manually, "
            "then re-run the migration."
        )
    op.create_index(
        _ORG_EMAIL_INDEX,
        "users",
        ["organization_id", sa.text("lower(email)")],
        unique=True,
    )

    # 3. The global index of 0020 goes: the same email may now exist in two orgs.
    op.drop_index(_GLOBAL_EMAIL_INDEX, table_name="users")


def downgrade() -> None:
    bind = op.get_bind()
    shared = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT lower(email) FROM users "
            "GROUP BY lower(email) HAVING count(*) > 1) AS d"
        )
    ).scalar_one()
    if shared:
        raise RuntimeError(
            f"0022 downgrade: {shared} email address(es) exist in more than one "
            "organization. The previous schema allows each email only once across "
            "all organizations; resolve them manually, then re-run the downgrade."
        )
    op.create_index(_GLOBAL_EMAIL_INDEX, "users", [sa.text("lower(email)")], unique=True)
    op.drop_index(_ORG_EMAIL_INDEX, table_name="users")
    op.drop_constraint(op.f(_SLUG_CHECK), "organizations", type_="check")
    op.drop_constraint(_SLUG_UNIQUE, "organizations", type_="unique")
    op.drop_column("organizations", "slug")
