"""Traveller grade on users (AIR-04 "SVPs and above"): users.grade + a ladder CHECK.

Revision ID: 0021_traveller_grades
Revises: 0020_notification_reads
Create Date: 2026-11-14

Schema only, safe on a populated database:

* ``users.grade TEXT NULL`` — a nullable add with no default, so Postgres records it
  in the catalog without rewriting the table.  Every existing user gets NULL
  (= grade unknown).  The migration NEVER guesses a grade for a real person: a
  finance admin sets it on the Users page (audited).  The demo seed backfills its
  own demo accounts only.
* ``ck_users_grade_on_ladder`` — ``grade IS NULL OR grade IN (<ladder codes>)``.
  Added ``NOT VALID`` and then validated, so the brief lock covers only the catalog
  change and the validation scan (all rows are NULL at this point, so it passes).

The ladder codes below are a FROZEN copy of ``config/constants/grades.py``
``GRADE_CODES`` at the time of this revision — never import them: a later change to
the ladder needs its own migration (``backend/tests/test_kl_grade_ladder.py`` fails
when the two differ).

``downgrade()`` drops the constraint, then the column (grades recorded since are lost;
the audit log keeps every change).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0021_traveller_grades"
down_revision: str | None = "0020_notification_reads"
branch_labels: str | None = None
depends_on: str | None = None

# FROZEN copy of config.constants.grades.GRADE_CODES (most senior first).
LADDER_CODES: tuple[str, ...] = (
    "CSUITE",
    "SVP",
    "VP",
    "DIRECTOR",
    "MANAGER",
    "SENIOR_IC",
    "IC",
    "INTERN",
)

_CONSTRAINT = "ck_users_grade_on_ladder"


def _check_sql() -> str:
    codes = ", ".join(f"'{c}'" for c in LADDER_CODES)
    return f"grade IS NULL OR grade IN ({codes})"


def upgrade() -> None:
    op.add_column("users", sa.Column("grade", sa.Text(), nullable=True))
    op.execute(
        f"ALTER TABLE users ADD CONSTRAINT {_CONSTRAINT} CHECK ({_check_sql()}) NOT VALID"
    )
    op.execute(f"ALTER TABLE users VALIDATE CONSTRAINT {_CONSTRAINT}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE users DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.drop_column("users", "grade")
