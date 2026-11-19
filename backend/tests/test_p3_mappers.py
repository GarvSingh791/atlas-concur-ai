"""Hermetic mapper-configuration test for P3-T1 models.

No Postgres required — this test purely imports all models and calls
configure_mappers() to verify that every relationship() / back_populates pair
resolves without errors.  Runs in hermetic CI.
"""

from __future__ import annotations


def test_configure_mappers_resolves() -> None:
    """All 21 ORM models (10 existing + 11 new) must configure without error.

    configure_mappers() raises if any back_populates string references a
    non-existent attribute or if relationship targets can't be resolved.
    This catches typos in back_populates, missing relationship declarations,
    and circular import issues.
    """
    from sqlalchemy.orm import configure_mappers

    import database.models  # noqa: F401  — side-effect: register all models

    # Raises sqlalchemy.exc.InvalidRequestError on misconfiguration.
    configure_mappers()
