"""Global search DTOs (product gap 3 — the top-bar command search)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel

SearchGroupKey = Literal["trips", "reports", "entries", "receipts", "approvals", "policy_rules"]


class SearchResult(BaseModel):
    """One hit.  ``link`` is the canonical in-app route for the subject;
    ``fallback_link`` (when present) is the approval view of the same subject,
    for viewers whose role cannot open ``link``'s page.  Either may be None when
    there is no page that shows the subject to this caller."""

    id: uuid.UUID
    title: str
    subtitle: str | None = None
    status: str | None = None
    link: str | None = None
    fallback_link: str | None = None


class SearchGroup(BaseModel):
    key: SearchGroupKey
    label: str
    items: list[SearchResult]
    # True when more matches exist than the per-group cap returned.
    truncated: bool = False


class SearchResponse(BaseModel):
    query: str
    groups: list[SearchGroup]
