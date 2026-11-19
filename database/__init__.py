"""Atlas data layer.

Owns the SQLAlchemy ORM foundation: the shared ``DeclarativeBase`` and the
composable convention mixins (``database.models``). Concrete schema models and
Alembic migrations land here in Phase 3 — this package only provides the
reusable building blocks they inherit.
"""
