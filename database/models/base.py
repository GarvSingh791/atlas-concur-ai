"""Declarative base and metadata naming convention for all Atlas ORM models.

A SINGLE ``Base`` (SQLAlchemy 2.0 ``DeclarativeBase``) is defined here and shared
by every model in Phase 3. The attached ``MetaData`` carries a deterministic
``naming_convention`` so that constraints and indexes get stable, predictable
names — this is what lets Alembic autogenerate (P3-T2) produce reproducible
migrations instead of randomly-named objects.

NAMING CONVENTION NOTE (important for Phase 3):
    The ``ck`` (check-constraint) pattern interpolates ``%(constraint_name)s``.
    SQLAlchemy can only fill that in if the ``CheckConstraint`` was created with
    an explicit ``name=`` — an unnamed check raises at DDL-render time. Therefore
    EVERY ``CheckConstraint`` added to a model (e.g. ``amount >= 0``,
    ``end_date >= start_date``) MUST pass an explicit ``name=``.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Canonical SQLAlchemy/Alembic naming convention. Keep these patterns stable —
# changing them later would rename existing constraints/indexes in migrations.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for every Atlas ORM model.

    All models inherit from this single ``Base`` so they register on one
    ``MetaData`` (with the shared naming convention) and one mapper registry.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
